#!/usr/bin/env python

import glob
import luigi
import numpy as np
import os
from pmx.scripts.workflows.SGE_tasks.SGETunedJobTask import SGETunedJobTask #tuned for the owl cluster
from pmx import ndx
from pmx.model import Model
from luigi.parameter import ParameterVisibility
from pmx.scripts.workflows.fit_ligs_multiframes_python3 import fit,rotate_velocities_R, find_last_protein_atom
from pmx.scripts.workflows.SGE_tasks.absFE.LinP.restraints import Task_PL_gen_restraints
from pmx.scripts.workflows.SGE_tasks.absFE.LinW.equil_sims import Sim_WL_NPT
from pmx.scripts.workflows.SGE_tasks.absFE.ApoP.equil_sims import Sim_ApoP_NPT
from pmx.scripts.workflows.SGE_tasks.absFE.LinP.prep_folders import Prep_PL_folder
from pmx.scripts.workflows.utils import read_from_mdp
from pmx.xtc import Trajectory


# ==============================================================================
#                         Derivative Task Classes
# ==============================================================================
class Task_PL_gen_morphes(SGETunedJobTask):

    #Parameters:
    p = luigi.Parameter(description='Protein name')
    l = luigi.Parameter(description='Ligand name')
    i = luigi.IntParameter(description='Repeat number')
    m = luigi.IntParameter(description='Sampling sim number')
    sTI = luigi.Parameter(description='Coupling state for TI')

    folder_path = luigi.Parameter(significant=False,
                 visibility=ParameterVisibility.HIDDEN,
                 description='Path to the protein+ligand folder to set up')

    study_settings = luigi.DictParameter(significant=False,
                 visibility=ParameterVisibility.HIDDEN,
                 description='Dict of study stettings '
                 'used to propagate settings to dependencies')

    restr_scheme = luigi.Parameter(significant=True,
                 description='Restraint scheme to use. '
                 'Aligned, Fitted or Fixed')

    stage="morphes"

    #request 1 cores
    n_cpu = luigi.IntParameter(visibility=ParameterVisibility.HIDDEN,
                               default=1, significant=False)

    #avoid Prameter not a string warnings
    job_name_format = luigi.Parameter(
        visibility=ParameterVisibility.HIDDEN,
        significant=False, default="pmx_{task_family}_p{p}_l{l}_{sTI}{i}_{m}",
        description="A string that can be "
        "formatted with class variables to name the job with qsub.")

    def __init__(self, *args, **kwargs):
        super(Task_PL_gen_morphes, self).__init__(*args, **kwargs)
        self._setupState()

        #set variables
        self.base_path = self.study_settings['base_path']
        self.sim_path = self.folder_path+"/state%s/repeat%d/%s%d"%(
            self.sTI, self.i, self.stage, self.m)
        self.mdp = self.study_settings['mdp_path'] +\
            "/protein/eq_npt_{0}.mdp".format(
                self.study_settings['TIstates'][self.sTI])

    def _setupState(self):
        self.s=self.sTI

    def work(self):
        #generate morphs for A state
        os.makedirs(self.sim_path, exist_ok=True)
        os.chdir(self.sim_path)

        tpr=self.folder_path+"/state{2}/repeat{3}/npt{4}/tpr.tpr".format(
            self.p, self.l, self.s, self.i, self.m)
        trj=self.folder_path+"/state{2}/repeat{3}/npt{4}/traj.trr".format(
            self.p, self.l, self.s, self.i, self.m)

        #this is slow
        os.system("echo 0 | gmx trjconv -s %s "
                  "-f %s -o %s "
                  "-b %f -sep -ur compact -pbc mol "
                  "> /dev/null 2>&1"%(tpr,trj,"frame.gro",self.study_settings['b']) )

        cleanList = glob.glob(self.folder_path+'/#*')
        for filePath in cleanList:
            try:
                os.unlink(filePath)
            except:
                print("Error while deleting file: ", filePath)

        #restore base path
        os.chdir(self.base_path)


    def requires(self):
        #restraints require both state A & B for all repeats and sampling sims
        return( Task_PL_gen_restraints(p=self.p, l=self.l,
                          study_settings=self.study_settings,
                          folder_path=self.folder_path,
                          parallel_env=self.parallel_env,
                          restr_scheme=self.restr_scheme) )

    def output(self):
        #find nframes by reading the mdp file
        end_time, dtframe = read_from_mdp(self.mdp)
        nframes=int(end_time/dtframe) - int(self.study_settings['b']/dtframe) +1 #first frame counts

        targets=[]
        for nf in range(nframes):
            targets.append(luigi.LocalTarget(
                os.path.join(self.sim_path, 'frame%d.gro'%nf)) )
        return targets




class Task_PL_align(Task_PL_gen_morphes):

    extra_packages=[np]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if(not (self.restr_scheme=="Aligned" or self.restr_scheme=="Aligned_crystal")):
            raise(Exception("{} should only be used if restraint "
                            "scheme is {}".format(
                                self.__class__.__name__ ,"Aligned")))

    def _setupState(self):
        if(self.sTI != "C"):
            raise(ValueError("Aligning morphes for TI state{}. "
                     "{} should only be done on TI stateC.".format(
                         self.sTI, self.__class__.__name__)))
            exit(1);
        # no need to set self.s as it is never used
        # else:
        #     self.s="B" # TI stateC depends on NPT sim in stateB

    def requires(self):
        #restraints require both state A & B for all repeats and sampling sims
        tasks=[Task_PL_gen_restraints(p=self.p, l=self.l,
                          study_settings=self.study_settings,
                          folder_path=self.folder_path,
                          parallel_env=self.parallel_env,
                          restr_scheme=self.restr_scheme)]

        for i in range(self.study_settings['n_repeats']):
            for m in range(self.study_settings['n_sampling_sims']):
                tasks.append(Sim_WL_NPT(l=self.l, i=i, m=m, s='B',
                          study_settings=self.study_settings,
                          folder_path=self.base_path+"/water/lig_{}".format(self.l),
                          parallel_env=self.parallel_env))

        return(tasks)


    def work(self):
        #make the C state
        os.makedirs(self.sim_path, exist_ok=True)
        os.chdir(self.sim_path)

        trj_A_src=self.folder_path+"/state{2}/repeat{3}/npt{4}/".format(
            self.p, self.l, 'A', self.i, self.m)  #P+L
        trj_B_src=self.base_path+"/prot_{0}/apoP/repeat{3}/npt{4}/".format(
            self.p, self.l, None, self.i, self.m) #apoP
        trj_C_src=self.base_path+"/water/lig_{1}/state{2}/repeat{3}/npt{4}/".format(
            self.p, self.l, 'B', self.i, self.m)  #vacL

        #Cut the begining off of trjs and center them
        os.system("echo Protein 0 | gmx trjconv -s {tpr} -f {trj} -o trj_A.trr "
                  "-b {b} -ur compact -pbc mol -center "
                  "> align.log 2>&1".format(
                      tpr=trj_A_src+"tpr.tpr", trj=trj_A_src+"traj.trr",
                      b=self.study_settings['b']) ) #P+L
        os.system("echo Protein 0 | gmx trjconv -s {tpr} -f {trj} -o trj_B.trr "
                  "-b {b} -ur compact -pbc mol -center "
                  ">> align.log 2>&1".format(
                      tpr=trj_B_src+"tpr.tpr", trj=trj_B_src+"traj.trr",
                      b=self.study_settings['b']) ) #apoP
        os.system("echo 2 0 | gmx trjconv -s {tpr} -f {trj} -o trj_C.trr "
                  "-b {b} -ur compact -pbc mol -center "
                  ">> align.log 2>&1".format(
                      tpr=trj_C_src+"tpr.tpr", trj=trj_C_src+"traj.trr",
                      b=self.study_settings['b']) ) #vacL


        m_A = Model(self.folder_path+"/ions%d_%d.pdb"%(self.i,self.m),bPDBTER=True)
        m_B = Model(self.base_path+"/prot_{0}/apoP/ions{3}_{4}.pdb".format(
            self.p, self.l, None, self.i, self.m),bPDBTER=True) #apoP
        m_C = Model(self.base_path+"/water/lig_{1}/ions{3}_{4}.pdb".format(
            self.p, self.l, 'B', self.i, self.m),bPDBTER=True) #vacL
        m_A.a2nm()
        m_B.a2nm()
        m_C.a2nm()

        chID,resID = find_last_protein_atom( m_B )

        trj_A = Trajectory("trj_A.trr") #P+L
        trj_B = Trajectory("trj_B.trr") #apoP
        trj_C = Trajectory("trj_C.trr") #vacL

        ndx_file_A = ndx.IndexFile(self.folder_path+"/index_prot_mol.ndx", verbose=False)
        ndx_file_C = ndx.IndexFile(self.base_path+"/water/lig_{}/index.ndx".format(self.l), verbose=False)
        p_ndx = np.asarray(ndx_file_A["Protein"].ids)-1
        linA_ndx = np.asarray(ndx_file_A["MOL"].ids)-1
        l_ndx = np.asarray(ndx_file_C["MOL"].ids)-1

        #Frames are not acessible individually, just in sequence.
        #pmx.xtc.Trajectory is based on __iter__, so we need a custom
        #"for" loop to simultaneously go through both trajectories.
        #Based on https://www.programiz.com/python-programming/iterator
        iter_A = iter(trj_A)
        iter_B = iter(trj_B)
        iter_C = iter(trj_C)
        fridx=0
        while True:
            try:
                frame_A = next(iter_A)
                frame_B = next(iter_B)
                frame_C = next(iter_C)
            except StopIteration:
                break

            #don't want frames while still equilibrating
            if(frame_A.time<self.study_settings['b']):
                continue

            if(not os.path.isfile("frame%d.gro"%fridx)):
                frame_A.update(m_A)
                frame_B.update(m_B, uv=True)
                frame_C.update(m_C, uv=True)

                # m_A.write("m_A1.gro")
                # step1: fit prot from prot+lig onto apo protein
                (v1,v2,R) = fit( m_B, m_A, p_ndx, p_ndx )
                # rotate velocities
                # not needed. We aren't saving m_A

                # step2: ligand onto the ligand from prot+lig structure
                (v1,v2,R) = fit( m_A, m_C, linA_ndx, l_ndx )
                # rotate velocities
                rotate_velocities_R( m_C, R )

                #insert vac ligand into B
                m_B.insert_residue(resID, m_C.residues[0], chID)

                # output
                m_B.write("frame%d.gro"%fridx)

                #m_b needs to be reloaded to have correct # of atoms next iteration
                m_B = Model(self.base_path+"/prot_{0}/apoP/ions{3}_{4}.pdb".format(
                    self.p, self.l, None, self.i, self.m),bPDBTER=True) #apoP
                m_B.a2nm()

            fridx+=1

        #restore base path
        os.chdir(self.base_path)



class Task_PL_align2crystal(Task_PL_align):
    def __init__(self, *args, **kwargs):
        super(Task_PL_align,self).__init__(*args, **kwargs)
        if(self.restr_scheme!="Aligned_crystal"):
            raise(Exception("{} should only be used if restraint "
                            "scheme is {}".format(
                                self.__class__.__name__ ,"Aligned_crystal")))

    def _setupState(self):
        if(self.sTI != "D"):
            raise(ValueError("Aligning morphes to crystal structure for TI state{}. "
                     "{} should only be done on TI stateD.".format(
                         self.sTI, self.__class__.__name__)))
            exit(1);
        # no need to set self.s as it is never used
        # else:
        #     self.s="B" # TI stateC depends on NPT sim in stateB

    def requires(self):
        tasks=[]
        for i in range(self.study_settings['n_repeats']):
            for m in range(self.study_settings['n_sampling_sims']):
                tasks.append(Sim_WL_NPT(l=self.l, i=i, m=m, s='B',
                          study_settings=self.study_settings,
                          folder_path=self.base_path+"/water/lig_{}".format(self.l),
                          parallel_env=self.parallel_env))
                tasks.append(Sim_ApoP_NPT(p=self.p, i=i, m=m,
                          study_settings=self.study_settings,
                          folder_path=self.base_path+"/prot_{}/apoP".format(self.p),
                          parallel_env=self.parallel_env))
                tasks.append(Prep_PL_folder(p=self.p, l=self.l,
                          study_settings=self.study_settings,
                          folder_path=self.folder_path))

        return(tasks)

    def _overwrite_coords(atoms_trg, atoms_src):
        if(len(atoms_trg)!=len(atoms_src)):
            raise(Exception("Copying coordinates between systems with "
                            "different numbers of atoms ({} and{})".format(
                                len(atoms_trg), len(atoms_src) )))
        for i in len(atoms_src):
            for r in range(3):
                atoms_trg[i].x[r] = atoms_src[i].x[r]


    def work(self):
        #make the C state
        os.makedirs(self.sim_path, exist_ok=True)
        os.chdir(self.sim_path)

        trj_B_src=self.base_path+"/prot_{0}/apoP/repeat{3}/npt{4}/".format(
            self.p, self.l, None, self.i, self.m) #apoP
        trj_C_src=self.base_path+"/water/lig_{1}/state{2}/repeat{3}/npt{4}/".format(
            self.p, self.l, 'B', self.i, self.m)  #vacL

        #Cut the begining off of trjs and center them
        os.system("echo Protein 0 | gmx trjconv -s {tpr} -f {trj} -o trj_B.trr "
                  "-b {b} -ur compact -pbc mol -center "
                  ">> align.log 2>&1".format(
                      tpr=trj_B_src+"tpr.tpr", trj=trj_B_src+"traj.trr",
                      b=self.study_settings['b']) ) #apoP
        os.system("echo 2 0 | gmx trjconv -s {tpr} -f {trj} -o trj_C.trr "
                  "-b {b} -ur compact -pbc mol -center "
                  ">> align.log 2>&1".format(
                      tpr=trj_C_src+"tpr.tpr", trj=trj_C_src+"traj.trr",
                      b=self.study_settings['b']) ) #vacL


        m_A = Model(self.folder_path+"/ions%d_%d.pdb"%(self.i,self.m),bPDBTER=True)
        m_A_ref = Model(self.folder_path+"/ions%d_%d.pdb"%(self.i,self.m),bPDBTER=True)
        m_B = Model(self.base_path+"/prot_{0}/apoP/ions{3}_{4}.pdb".format(
            self.p, self.l, None, self.i, self.m),bPDBTER=True) #apoP
        m_C = Model(self.base_path+"/water/lig_{1}/ions{3}_{4}.pdb".format(
            self.p, self.l, 'B', self.i, self.m),bPDBTER=True) #vacL
        m_A.a2nm()
        m_A_ref.a2nm()
        m_B.a2nm()
        m_C.a2nm()

        chID,resID = find_last_protein_atom( m_B )

        trj_B = Trajectory("trj_B.trr") #apoP
        trj_C = Trajectory("trj_C.trr") #vacL

        #make index_prot_mol.ndx here. Before it was done during restraint gen.
        os.system("echo \"1|13\nq\n\" | "
                  "gmx make_ndx -f {fp}/ions0_0.pdb "
                  "-o {fp}/index_prot_mol.ndx > /dev/null 2>&1".format(
                      fp=self.folder_path ))

        ndx_file_A = ndx.IndexFile(self.folder_path+"/index_prot_mol.ndx", verbose=False)
        ndx_file_C = ndx.IndexFile(self.base_path+"/water/lig_{}/index.ndx".format(self.l), verbose=False)
        p_ndx = np.asarray(ndx_file_A["Protein"].ids)-1
        linA_ndx = np.asarray(ndx_file_A["MOL"].ids)-1
        l_ndx = np.asarray(ndx_file_C["MOL"].ids)-1

        #Frames are not acessible individually, just in sequence.
        #pmx.xtc.Trajectory is based on __iter__, so we need a custom
        #"for" loop to simultaneously go through both trajectories.
        #Based on https://www.programiz.com/python-programming/iterator
        iter_B = iter(trj_B)
        iter_C = iter(trj_C)
        fridx=0
        while True:
            try:
                frame_B = next(iter_B)
                frame_C = next(iter_C)
            except StopIteration:
                break

            #don't want frames while still equilibrating
            #this is already handled by trjconv
            # if(frame_B.time<self.study_settings['b']):
            #     continue

            if(not os.path.isfile("frame%d.gro"%fridx)):
                #m_A needs to be re-read or rotated back
                self._overwrite_coords(m_A.atoms, m_A_ref.atoms)
                #m_b needs to be reloaded to have correct # of atoms
                m_B = Model(self.base_path+"/prot_{0}/apoP/ions{3}_{4}.pdb".format(
                    self.p, self.l, None, self.i, self.m),bPDBTER=True) #apoP
                m_B.a2nm()
                frame_B.update(m_B, uv=True)
                frame_C.update(m_C, uv=True)


                # step1: fit prot from prot+lig onto apo protein
                (v1,v2,R) = fit( m_B, m_A, p_ndx, p_ndx )
                # rotate velocities
                # not needed. We aren't saving m_A

                # step2: ligand onto the ligand from prot+lig structure
                (v1,v2,R) = fit( m_A, m_C, linA_ndx, l_ndx )
                # rotate velocities
                rotate_velocities_R( m_C, R )

                #insert vac ligand into B
                m_B.insert_residue(resID, m_C.residues[0], chID)

                # output
                m_B.write("frame%d.gro"%fridx)




            fridx+=1

        #restore base path
        os.chdir(self.base_path)