"""

gadgethdf
=========


Implementation of backend reader for GadgetHDF files by Andrew Pontzen.

The gadget array names are mapped into pynbody array names according
to the mappings given by the config.ini section [gadgethdf-name-mapping].

The gadget particle groups are mapped into pynbody families according
to the mappings specified by the config.ini section [gadgethdf-type-mapping].
This can be many-to-one (many gadget particle types mapping into one
pynbody family), but only datasets which are common to all gadget types
will be available from pynbody.

Spanned files are supported. To load a range of files snap.0, snap.1, ... snap.n,
pass the filename 'snap'. If you pass snap.0, only that particular file will
be loaded.
"""


from __future__ import with_statement  # for py2.5

from . import snapshot, array, util
from . import family
from . import units
from . import config
from . import config_parser
from . import halo

import ConfigParser

import struct
import os
import numpy as np
import functools
import warnings
import sys
import weakref 

try:
    import h5py
except ImportError:
    h5py = None

_type_map = {}
for x in family.family_names():
    try:
        _type_map[family.get_family(x)] = \
                 [q for q in config_parser.get(
                     'gadgethdf-type-mapping', x).split(",")]
    except ConfigParser.NoOptionError:
        pass

_name_map, _rev_name_map = util.setup_name_maps('gadgethdf-name-mapping')
_translate_array_name = util.name_map_function(_name_map, _rev_name_map)


def _append_if_array(to_list, name, obj):
    if not hasattr(obj, 'keys'):
        to_list.append(name)


class DummyHDFData(object):
    """A stupid class to allow emulation of mass arrays for particles
    whose mass is in the header"""
    def __init__(self, value, length):
        self.value = value
        self.length = length
        self.shape = (length, )
        self.dtype = np.dtype(float)

    def __len__(self):
        return self.length

    def read_direct(self, target):
        target[:] = self.value


class GadgetHDFSnap(snapshot.SimSnap):
    """
    Class that reads HDF Gadget data
    """

    def __init__(self, filename):

        global config
        super(GadgetHDFSnap, self).__init__()

        self._filename = filename

        if not h5py.is_hdf5(filename):
            h1 = h5py.File(filename+".0.hdf5", "r")
            numfiles = h1['Header'].attrs['NumFilesPerSnapshot']
            self._hdf = [h5py.File(filename+"."+str(
                i)+".hdf5", "r") for i in xrange(numfiles)]
        else:
            self._hdf = [h5py.File(filename, "r")]

        self._family_slice = {}

        self._loadable_keys = set([])
        self._family_arrays = {}
        self._arrays = {}
        self.properties = {}

        # determine which particle types are in the output

        my_type_map = {}

        for fam, g_types in _type_map.iteritems() : 
            my_types = []
            for x in g_types :
                if x in self._hdf[0].keys() : 
                    my_types.append(x)
            if len(my_types) : 
                my_type_map[fam] = my_types
        
        sl_start = 0
        for x in my_type_map:
            l = 0
            for name in my_type_map[x]:
                for hdf in self._hdf:
                    l += hdf[name]['Coordinates'].shape[0]
            self._family_slice[x] = slice(sl_start, sl_start+l)

            k = self._get_hdf_allarray_keys(self._hdf[0][name])
            self._loadable_keys = self._loadable_keys.union(set(k))
            sl_start += l

        self._loadable_keys = [_translate_array_name(
            x, reverse=True) for x in self._loadable_keys]
        self._num_particles = sl_start

        self._my_type_map = my_type_map

        self._decorate()

    def _family_has_loadable_array(self, fam, name, subgroup = None):
        """Returns True if the array can be loaded for the specified family.
        If fam is None, returns True if the array can be loaded for all families."""

        if name == "mass":
            return True

        if subgroup is None : 
            hdf = self._hdf[0]
        else : 
            hdf = self._hdf[0][subgroup]


        if fam is None:
            return all([self._family_has_loadable_array(fam_x, name, subgroup) for fam_x in self._family_slice])

        else:
            translated_name = _translate_array_name(name)
            for n in self._my_type_map[fam]:
                if translated_name not in self._get_all_particle_arrays(n,subgroup) :
                    return False
            return True

    def _get_all_particle_arrays(self, gtype, subgroup=None): 
        """Return all array names for a given gadget particle type"""

        # this is a hack to flatten a list of lists
        if subgroup is not None : 
            l = [item for sublist in [self._get_hdf_allarray_keys(x[subgroup][gtype]) for x in self._hdf] for item in sublist]
        else : 
            l = [item for sublist in [self._get_hdf_allarray_keys(x[gtype]) for x in self._hdf] for item in sublist]

        # now just return the unique items by converting to a set
        return list(set(l))

    def loadable_keys(self, fam=None):
        return self._loadable_keys

    @staticmethod
    def _write(self, filename=None):
        raise RuntimeError("Not implemented")

        global config

        with self.lazy_off:  # prevent any lazy reading or evaluation

            if filename is None:
                filename = self._filename

            if config['verbose']:
                print>>sys.stderr, "GadgetHDF: writing main file as", filename

            self._hdf_out = h5py.File(filename, "w")

    def _write_array(self, array_name, filename=None):
        raise RuntimeError("Not implemented")

    @staticmethod
    def _get_hdf_allarray_keys(group):
        """Return all HDF array keys underneath group (includes nested groups)"""
        k = []
        group.visititems(functools.partial(_append_if_array, k))
        return k

    @staticmethod
    def _get_hdf_dataset(particle_group, hdf_name):
        """Return the HDF dataset resolving /'s into nested groups, and returning
        an apparent Mass array even if the mass is actually stored in the header"""

        if hdf_name == "Mass":
            try:
                pgid = int(particle_group.name[-1])
                mtab = particle_group.parent['Header'].attrs['MassTable'][pgid]
                if mtab > 0:
                    return DummyHDFData(mtab, particle_group['Coordinates'].shape[0])
            except (IndexError, KeyError):
                pass

        ret = particle_group
        for tpart in hdf_name.split("/"):
            ret = ret[tpart]
        return ret

    @staticmethod
    def _get_cosmo_factors(hdf, arr_name) :
        """Return the cosmological factors for a given array"""
        match = [s for s in GadgetHDFSnap._get_hdf_allarray_keys(hdf) if ((arr_name in s) & ('PartType' in s))]
        if len(match) > 0 : 
            a_exp = hdf[match[0]].attrs['aexp-scale-exponent']
            h_exp = hdf[match[0]].attrs['h-scale-exponent']
            return units.a**a_exp, units.h**h_exp
        else : 
            return units.Unit('1.0'), units.Unit('1.0')


    def _load_array(self, array_name, fam=None, subgroup = None):
        if not self._family_has_loadable_array(fam, array_name, subgroup):
            raise IOError("No such array on disk")
        else:
            if fam is not None:
                famx = fam
            else:
                famx = self._family_slice.keys()[0]

            translated_name = _translate_array_name(array_name)

            
            # this next chunk of code is just to determine the
            # dimensionality of the data

            i=0
            not_loaded = True

            # not all arrays are present in all hdfs so need to loop
            # until we find one
            while(not_loaded) :
                if subgroup is None : 
                    hdf0 = self._hdf[i]
                else : 
                    hdf0 = self._hdf[i][subgroup]
                try : 
                    dset0 = self._get_hdf_dataset(hdf0[
                        self._my_type_map[famx][0]], translated_name)
                    not_loaded = False
                except KeyError: 
                    i+=1

            assert len(dset0.shape) <= 2
            dy = 1
            if len(dset0.shape) > 1:
                dy = dset0.shape[1]

            # check if the dimensions make sense -- if
            # not, assume we're looking at an array that
            # is 3D and cross your fingers
            npart = len(hdf0[self._my_type_map[famx][0]]['ParticleIDs'])
            if len(dset0) != npart :
                dy = len(dset0)/npart                     

            dtype = dset0.dtype

            # got the dimension -- now make the arrays and load the data

            if fam is None:
                self._create_array(array_name, dy, dtype=dtype)
                self[array_name].set_default_units()
            else:
                self[fam]._create_array(array_name, dy, dtype=dtype)
                self[fam][array_name].set_default_units()

            if fam is not None:
                fams = [fam]
            else:
                fams = self._family_slice.keys()

            for f in fams:
                i0 = 0
                for t in self._my_type_map[f]:
                    for hdf in self._hdf:
                        if subgroup is not None : 
                            hdf = hdf[subgroup]
                        try : 
                            npart = len(hdf[t]['ParticleIDs'])
                        except KeyError: 
                            npart = 0
                        
                        if npart > 0 : 
                            dataset = self._get_hdf_dataset(hdf[t], translated_name)

                            # check if the dimensions make sense -- if
                            # not, assume we're looking at an array that
                            # is 3D and cross your fingers
                            if len(dataset) != npart : 
                                temp = dataset[:].reshape((len(dataset)/3,3))
                                i1 = i0+len(temp)
                                self[f][array_name][i0:i1] = temp
                            
                            else : 
                                i1 = i0+len(dataset)
                                dataset.read_direct(self[f][array_name][i0:i1])

                            i0 = i1

    @staticmethod
    def _can_load(f):
        try:
            if h5py.is_hdf5(f) or h5py.is_hdf5(f+".0.hdf5") and ('sub' not in f):
                return True
            else:
                return False
        except AttributeError:
            if "hdf5" in f:
                warnings.warn(
                    "It looks like you're trying to load HDF5 files, but python's HDF support (h5py module) is missing.", RuntimeWarning)
            return False


@GadgetHDFSnap.decorator
def do_properties(sim):
    atr = sim._hdf[0]['Header'].attrs
    
    # expansion factor could be saved as redshift
    try:
        sim.properties['a'] = atr['ExpansionFactor']
    except KeyError : 
        sim.properties['a'] = 1./(1+atr['Redshift'])

    # time unit might not be set in the attributes
    try : 
        sim.properties['time'] = units.Gyr*atr['Time_GYR']
    except KeyError: 
        pass
        
    # not all omegas need to be specified in the attributes
    try : 
        sim.properties['omegaB0'] = atr['OmegaBaryon']
    except KeyError : 
        pass

    sim.properties['omegaM0'] = atr['Omega0']
    sim.properties['omegaL0'] = atr['OmegaLambda']
    sim.properties['boxsize'] = atr['BoxSize']
    sim.properties['z'] = (1./sim.properties['a'])-1
    sim.properties['h'] = atr['HubbleParam']
    for s in sim._hdf[0]['Header'].attrs:
        if s not in ['ExpansionFactor', 'Time_GYR', 'Omega0', 'OmegaBaryon', 'OmegaLambda', 'BoxSize', 'HubbleParam']:
            sim.properties[s] = sim._hdf[0]['Header'].attrs[s]


@GadgetHDFSnap.decorator
def do_units(sim):
    
    # this doesn't seem to be standard -- maybe use the convention
    # from tipsy.py and set cosmo = True if there is a hubble constant
    # specified?
    try : 
        cosmo = (sim._hdf[0]['Parameters'][
            'NumericalParameters'].attrs['ComovingIntegrationOn']) != 0
    except KeyError : 
        cosmo = 'HubbleParam' in sim._hdf[0]['Header'].attrs.keys()

    try : 
        atr = sim._hdf[0]['Units'].attrs
    except KeyError : 
        warnings.warn("No unit information found: using defaults.",RuntimeWarning)
        sim._file_units_system = [units.Unit(x) for x in ('G', '1 kpc', '1e10 Msol')]
        return

    vel_unit = atr['UnitVelocity_in_cm_per_s']*units.cm/units.s
    dist_unit = atr['UnitLength_in_cm']*units.cm
    mass_unit = atr['UnitMass_in_g']*units.g
    if cosmo:
        for fac in GadgetHDFSnap._get_cosmo_factors(sim._hdf[0],'Coordinates') : dist_unit *= fac
        for fac in GadgetHDFSnap._get_cosmo_factors(sim._hdf[0],'Velocity') : vel_unit *= fac
        for fac in GadgetHDFSnap._get_cosmo_factors(sim._hdf[0],'Mass') : mass_unit *= fac

    sim._file_units_system = [units.Unit(x) for x in [
                              vel_unit, dist_unit, mass_unit, "K"]]



###################
# SubFindHDF class
###################

class SubFindHDFSnap(GadgetHDFSnap) : 
    """
    Class to read Gadget's SubFind HDF data
    """

    def __init__(self, filename) : 
        
        global config
        super(SubFindHDFSnap,self).__init__(filename)

        # the super constructor does almost nothing because most of
        # the relevant data on particles is stored in the FOF group
        # attributes -- but it does set up the array and slice
        # dictionaries and the properties dictionary

        # get the properties from the FOF HDF group and other metadata
        self._decorate()

        # load the rest of the hdfs if the user doesn't specify a single hdf
        if not h5py.is_hdf5(filename) :
            numfiles = self.properties['NTask']
            self._hdf = [h5py.File(filename+"."+str(
                i)+".hdf5", "r") for i in xrange(numfiles)]
        
        # set up the particle type mapping
        my_type_map = {}

        for fam, g_types in _type_map.iteritems() : 
            my_types = []
            for x in g_types :
                if x in self._hdf[0]['FOF'].keys() : 
                    my_types.append(x)
            if len(my_types) : 
                my_type_map[fam] = my_types
        
        # set up family slices
        sl_start = 0
        self._loadable_keys = set([])
        for x in my_type_map:
            l = 0
            for name in my_type_map[x]:
                for hdf in self._hdf:
                    l += hdf['FOF'].attrs['Number_per_Type'][int(name[-1])]
            self._family_slice[x] = slice(sl_start, sl_start+l)

            k = self._get_hdf_allarray_keys(self._hdf[0]['FOF'][name])
            self._loadable_keys = self._loadable_keys.union(set(k))
            sl_start += l
        self._loadable_keys = [_translate_array_name(
            x, reverse=True) for x in self._loadable_keys]
        
        self._num_particles = sl_start

        self._my_type_map = my_type_map


    def _load_array(self, array_name, fam=None, subgroup = 'FOF') : 
        return GadgetHDFSnap._load_array(self, array_name, fam, subgroup)

    def halos(self) : 
        return halo.SubFindHDFHaloCatalogue(self)

    @staticmethod
    def _can_load(f):
        try:
            if h5py.is_hdf5(f) or h5py.is_hdf5(f+".0.hdf5"):
                return True
            else:
                return False
        except AttributeError:
            if "hdf5" in f:
                warnings.warn(
                    "It looks like you're trying to load HDF5 files, but python's HDF support (h5py module) is missing.", RuntimeWarning)
            return False

            
@SubFindHDFSnap.decorator
def do_properties(sim): 

    atr = sim._hdf[0]['FOF'].attrs

    for s in atr : 
        sim.properties[s] = atr[s]


# From the Tipsysnap example we replicate the various metalicity 
#
# calculate the number fraction YH, YHe as a function of metalicity. Cosmic 
# production rate of helium relative to metals (in mass)  
# delta Y/delta Z = 2.1 and primordial He Yp = 0.236 (Jimenez et al. 2003, 
# Science 299, 5612. 
#  piecewise linear
#  Y = Yp + dY/dZ*ZMetal up to ZMetal = 0.1, then linear decrease to 0 at Z=1)  

#  SUM_Metal = sum(ni/nH *mi),it is a fixed number for cloudy abundance. 
#  Massfraction fmetal = Z*SUM_metal/(1 + 4*nHe/nH + Z*SUM_metal) (1)
#  4*nHe/nH = mHe/mH = fHe/fH 
#  also fH + fHe + fMetal = 1  (2)
#  if fHe specified, combining the 2 eq above will solve for 
#  fH and fMetal 
        
def _abundance_estimator(metal) :

    Y_He = ((0.236+2.1*metal)/4.0)*(metal<=0.1)
    Y_He+= ((-0.446*(metal-0.1)/0.9+0.446)/4.0)*(metal>0.1)
    Y_H = 1.0-Y_He*4. - metal

    return Y_H, Y_He

@SubFindHDFSnap.derived_quantity
def HII(sim) :
    """Number of HII ions per proton mass"""
    Y_H, Y_He = _abundance_estimator(sim["Metallicity"])
    return Y_H - sim["HI"]

#@SubFindHDFSnap.derived_quantity
#def HeIII(sim) :
#    """Number of HeIII ions per proton mass"""
#    Y_H, Y_He = _abundance_estimator(sim["Metallicity"])
#    return Y_He-sim["HeII"]-sim["HeI"]

#@SubFindHDFSnap.derived_quantity
#def ne(sim) :
#    """Number of electrons per proton mass"""
#    return sim["HII"] + sim["HeII"] + 2*sim["HeIII"]

@SubFindHDFSnap.derived_quantity
def hetot(self) :
    return self['He']

@SubFindHDFSnap.derived_quantity
def hydrogen(self) :
    return self['H']

## Need to use the ionisation fraction calculation here which gives ionisation fraction
## based on the gas temperature, density and redshift for a CLOUDY table

@SubFindHDFSnap.derived_quantity
def HI(sim) :
    """Fraction of Neutral Hydrogen HI"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='hi')

@SubFindHDFSnap.derived_quantity
def OI(sim) :
    """Fraction of Oxygen OI"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='oi')

@SubFindHDFSnap.derived_quantity
def OII(sim) :
    """Fraction of Oxygen OII"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='oii')

@SubFindHDFSnap.derived_quantity
def OVI(sim) :
    """Fraction of Oxygen OVI"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='ovi')

@SubFindHDFSnap.derived_quantity
def CIV(sim) :
    """Fraction of Carbon CIV"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='civ')

@SubFindHDFSnap.derived_quantity
def NV(sim) :
    """Fraction of Nitrogen NV"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='nv')

@SubFindHDFSnap.derived_quantity
def SIV(sim) :
    """Fraction of Silicon SiIV"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='siiv')

@SubFindHDFSnap.derived_quantity
def MGII(sim) :
    """Fraction of Magnesium MgII"""

    import pynbody.analysis.ionfrac

    return pynbody.analysis.ionfrac.calculate(sim.g,ion='mgii')

#from .tipsy import TipsySnap
# Asplund et al (2009) ARA&A solar abundances (Table 1)
# m_frac = 10.0^([X/H] - 12)*M_X/M_H*0.74
# OR
# http://en.wikipedia.org/wiki/Abundance_of_the_chemical_elements      
# puts stuff more straighforwardly cites Arnett (1996)
# A+G from http://www.t4.lanl.gov/opacity/grevand1.html
# Anders + Grev (1989)    Asplund
XSOLFe=0.125E-2         # 1.31e-3
# Looks very wrong ([O/Fe] ~ 0.2-0.3 higher than solar), 
# probably because SN ejecta are calculated with
# Woosley + Weaver (1995) based on Anders + Grevesse (1989)
# XSOLO=0.59E-2           # 5.8e-2
XSOLO=0.84E-2
XSOLH=0.706             # 0.74
XSOLC=3.03e-3           # 2.39e-3
XSOLN=9.2e-4          # 7e-4
XSOLNe=1.66e-3          # 1.26e-3
XSOLMg=6.44e-4          # 7e-4
XSOLSi=7e-4          # 6.7e-4


@SubFindHDFSnap.derived_quantity
def feh(self) :
    minfe = np.amin(self['Fe'][np.where(self['Fe'] > 0)])
    self['Fe'][np.where(self['Fe'] == 0)]=minfe
    return np.log10(self['Fe']/self['H']) - np.log10(XSOLFe/XSOLH)

@SubFindHDFSnap.derived_quantity
def sixh(self) :
    minsi = np.amin(self['Si'][np.where(self['Si'] > 0)])
    self['Si'][np.where(self['Si'] == 0)]=minsi
    return np.log10(self['Si']/self['Si']) - np.log10(XSOLSi/XSOLH)

@SubFindHDFSnap.derived_quantity
def mgxh(self) :
    minmg = np.amin(self['Mg'][np.where(self['Mg'] > 0)])
    self['Mg'][np.where(self['Mg'] == 0)]=minmg
    return np.log10(self['Mg']/self['Mg']) - np.log10(XSOLMg/XSOLH)

@SubFindHDFSnap.derived_quantity
def oxh(self) :
    minox = np.amin(self['O'][np.where(self['O'] > 0)])
    self['O'][np.where(self['O'] == 0)]=minox
    return np.log10(self['O']/self['H']) - np.log10(XSOLO/XSOLH)

@SubFindHDFSnap.derived_quantity
def nexh(self) :
    minne = np.amin(self['Ne'][np.where(self['Ne'] > 0)])
    self['Ne'][np.where(self['Ne'] == 0)]=minne
    return np.log10(self['Ne']/self['Ne']) - np.log10(XSOLNe/XSOLH)

@SubFindHDFSnap.derived_quantity
def hexh(self) :
    minhe = np.amin(self['He'][np.where(self['He'] > 0)])
    self['He'][np.where(self['He'] == 0)]=minhe
    return np.log10(self['He']/self['He']) - np.log10(XSOLHe/XSOLH)

@SubFindHDFSnap.derived_quantity
def cxh(self) :
    mincx = np.amin(self['C'][np.where(self['C'] > 0)])
    self['C'][np.where(self['C'] == 0)]=mincx
    return np.log10(self['C']/self['H']) - np.log10(XSOLC/XSOLH)

@SubFindHDFSnap.derived_quantity
def nxh(self) :
    minnx = np.amin(self['N'][np.where(self['N'] > 0)])
    self['N'][np.where(self['N'] == 0)]=minnx
    return np.log10(self['N']/self['H']) - np.log10(XSOLH/XSOLH)

@SubFindHDFSnap.derived_quantity
def ofe(self) :
    minox = np.amin(self['O'][np.where(self['O'] > 0)])
    self['O'][np.where(self['O'] == 0)]=minox
    minfe = np.amin(self['Fe'][np.where(self['Fe'] > 0)])
    self['Fe'][np.where(self['Fe'] == 0)]=minfe
    return np.log10(self['O']/self['Fe']) - np.log10(XSOLO/XSOLFe)

@SubFindHDFSnap.derived_quantity
def mgfe(sim) :
    minmg = np.amin(sim['Mg'][np.where(sim['Mg'] > 0)])
    sim['Mg'][np.where(sim['Mg'] == 0)]=minmg
    minfe = np.amin(sim['Fe'][np.where(sim['Fe'] > 0)])
    sim['Fe'][np.where(sim['Fe'] == 0)]=minfe
    return np.log10(sim['Mg']/sim['Fe']) - np.log10(XSOLMg/XSOLFe)

@SubFindHDFSnap.derived_quantity
def nefe(sim) :
    minne = np.amin(sim['Ne'][np.where(sim['Ne'] > 0)])
    sim['Ne'][np.where(sim['Ne'] == 0)]=minne
    minfe = np.amin(sim['Fe'][np.where(sim['Fe'] > 0)])
    sim['Fe'][np.where(sim['Fe'] == 0)]=minfe
    return np.log10(sim['Ne']/sim['Fe']) - np.log10(XSOLNe/XSOLFe)

@SubFindHDFSnap.derived_quantity
def sife(sim) :
    minsi = np.amin(sim['Si'][np.where(sim['Si'] > 0)])
    sim['Si'][np.where(sim['Si'] == 0)]=minsi
    minfe = np.amin(sim['Fe'][np.where(sim['Fe'] > 0)])
    sim['Fe'][np.where(sim['Fe'] == 0)]=minfe
    return np.log10(sim['Si']/sim['Fe']) - np.log10(XSOLSi/XSOLFe)
