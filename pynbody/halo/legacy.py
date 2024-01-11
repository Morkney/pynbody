import os.path

import numpy as np

import pynbody.snapshot.simsnap

from . import Halo, HaloCatalogue


class RockstarIntermediateCatalogue(HaloCatalogue):
    """Reader for Rockstar intermediate catalogues as generated by
    Michael Tremmel's tool"""

    _halo_type = np.dtype([('id',np.int64),('num_p',np.int64),('indstart',np.int64)])
    _part_type = np.dtype('int64')

    def __init__(self, sim, sort=True, correct=False, **kwargs):
        assert isinstance(sim, pynbody.snapshot.simsnap.SimSnap)
        self._correct=correct
        HaloCatalogue.__init__(self, sim)

        self._halos = {}

        self._index_filename = sim.filename+".rockstar.halos"
        self._particles_filename = sim.filename+".rockstar.halo_particles_fpos"

        self._init_index()
        if sort:
            self._sort_index()

    def __len__(self):
        return self._nhalos

    def _get_particles_for_halo(self, num):
        halo_info = self._halo_info[num]
        with util.open_(self._particles_filename) as f:
            f.seek(halo_info['indstart']*self._part_type.itemsize)
            halo_ptcls=np.fromfile(f,dtype=self._part_type,count=halo_info['num_p']-1)
            halo_ptcls.sort()


        return halo_ptcls

    def _add_halo_id(self, halo, num):
        halo.properties['rockstar_halo_id']=self._halo_info[num]['id']
        if self._correct is True:
            with util.open_(self._particles_filename) as f:
                f.seek(self._halo_info['indstart'][num]*self._part_type.itemsize)
                one_ptcl = np.fromfile(f,dtype=self._part_type,count=1)
                from . import load
                onep = load(self.base.filename, take = one_ptcl)
                while not onep['grpid']:
                    one_ptcl = np.fromfile(f,dtype=self._part_type,count=1)
                    onep = load(self.base.filename, take = one_ptcl)
                halo.properties['rockstar_halo_id'] = onep['grpid'][0]

    def _get_halo(self, i):
        if self.base is None:
            raise RuntimeError("Parent SimSnap has been deleted")

        halo_ptcls = self._get_particles_for_halo(i)
        h = Halo(i, self, self.base, halo_ptcls)
        self._add_halo_id(h,i)
        return h

    def load_copy(self, i):
        """Load a fresh SimSnap with only the particles in halo i"""
        if i>=len(self):
            raise KeyError("No such halo")

        from . import load
        halo = load(self.base.filename, take=self._get_particles_for_halo(i))
        self._add_halo_id(halo, i)
        return halo

    @staticmethod
    def _can_load(sim,*args,**kwargs):
        return os.path.exists(sim.filename+".rockstar.halos") and \
               os.path.exists(sim.filename+".rockstar.halo_particles_fpos")

    def _init_index(self):
        with open(self._index_filename,"rb") as f:
            self._nhalos = np.fromfile(f,np.int64,1)[0]
            #self._halo_info = np.fromfile(f,self._halo_type,self._nhalos)
            self._halo_info = np.fromfile(f,self._halo_type,-1)
            ok, = np.where(self._halo_info['indstart']>=0)
            self._halo_info = self._halo_info[ok]

    def _sort_index(self):
        self._halo_info[::-1].sort(order='num_p')

    def get_group_array(self, family='star'):
        if family == 'star':
            target = self.base.star
        if family == 'gas':
            target = self.base.gas
        if family == 'dark':
            target = self.base.dark
        if family == 'BH':
            temptarget = self.base.star
            target = temptarget[(temptarget['tform']<0)]
        if family not in ['star','gas','dark', 'BH']:
            raise TypeError("family input not understood. Must be star, gas, or dark")
        ar = -1 * np.ones(len(target))
        if family != 'dark':
            fpos_ar = target.get_index_list(self.base)
        ngas = len(self.base.gas)
        ndark = len(self.base.dark)
        with util.open_(self._particles_filename) as f:
            for i in range(len(self._halo_info)):
                f.seek(self._halo_info[i]['indstart']*self._part_type.itemsize)
                halo_ptcls=np.fromfile(f,dtype=self._part_type,count=self._halo_info[i]['num_p']-1)
                if family == 'gas':
                    halo_ptcls = halo_ptcls[(halo_ptcls>=ndark)&(halo_ptcls<ndark+ngas)]
                if family == 'dark':
                    halo_ptcls = halo_ptcls[(halo_ptcls<ndark)]
                if family == 'star' or family == 'BH':
                    halo_ptcls = halo_ptcls[(halo_ptcls>=ndark+ngas)]
                if family != 'dark':
                    match, = np.where(np.in1d(fpos_ar, halo_ptcls))
                    ar[match] = i
                else:
                    ar[halo_ptcls] = i
        return ar.astype(np.int_)
