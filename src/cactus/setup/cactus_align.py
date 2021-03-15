#!/usr/bin/env python3

#Released under the MIT license, see LICENSE.txt

"""Run the multiple alignment on pairwise alignment input (ie cactus_setup_phase and beyond)

"""
import os
from argparse import ArgumentParser
import xml.etree.ElementTree as ET
import copy
import timeit
import time
import multiprocessing
from operator import itemgetter

from cactus.progressive.seqFile import SeqFile
from cactus.progressive.multiCactusTree import MultiCactusTree
from cactus.shared.common import setupBinaries, importSingularityImage
from cactus.progressive.cactus_progressive import exportHal
from cactus.progressive.multiCactusProject import MultiCactusProject
from cactus.shared.experimentWrapper import ExperimentWrapper
from cactus.progressive.schedule import Schedule
from cactus.progressive.projectWrapper import ProjectWrapper
from cactus.shared.common import cactusRootPath
from cactus.shared.configWrapper import ConfigWrapper
from cactus.pipeline.cactus_workflow import CactusWorkflowArguments
from cactus.pipeline.cactus_workflow import addCactusWorkflowOptions
from cactus.pipeline.cactus_workflow import CactusTrimmingBlastPhase
from cactus.pipeline.cactus_workflow import CactusSetupCheckpoint
from cactus.pipeline.cactus_workflow import prependUniqueIDs
from cactus.blast.blast import calculateCoverage
from cactus.shared.common import makeURL, catFiles
from cactus.shared.common import enableDumpStack
from cactus.shared.common import cactus_override_toil_options
from cactus.shared.common import findRequiredNode
from cactus.shared.common import getOptionalAttrib
from cactus.shared.common import cactus_call
from cactus.refmap import paf_to_lastz
from cactus.shared.common import write_s3, has_s3, get_aws_region

from toil.realtimeLogger import RealtimeLogger
from toil.job import Job
from toil.common import Toil
from toil.lib.bioio import logger
from toil.lib.bioio import setLoggingFromOptions
from toil.lib.threading import cpu_count

from sonLib.nxnewick import NXNewick
from sonLib.bioio import getTempDirectory, getTempFile

def main():
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    addCactusWorkflowOptions(parser)

    parser.add_argument("seqFile", help = "Seq file")
    parser.add_argument("cigarsFile", nargs="*", help = "Pairiwse aliginments (from cactus-blast, cactus-refmap or cactus-graphmap)")
    parser.add_argument("outHal", type=str, help = "Output HAL file (or directory in --batch mode)")
    parser.add_argument("--pathOverrides", nargs="*", help="paths (multiple allowd) to override from seqFile")
    parser.add_argument("--pathOverrideNames", nargs="*", help="names (must be same number as --paths) of path overrides")

    #Pangenome Options
    parser.add_argument("--pangenome", action="store_true",
                        help="Activate pangenome mode (suitable for star trees of closely related samples) by overriding several configuration settings."
                        " The overridden configuration will be saved in <outHal>.pg-conf.xml")
    parser.add_argument("--pafInput", action="store_true",
                        help="'cigarsFile' arugment is in PAF format, rather than lastz cigars.")
    parser.add_argument("--usePafSecondaries", action="store_true",
                        help="use the secondary alignments from the PAF input.  They are ignored by default.")
    parser.add_argument("--singleCopySpecies", type=str,
                        help="Filter out all self-alignments in given species")
    parser.add_argument("--barMaskFilter", type=int, default=None,
                        help="BAR's POA aligner will ignore softmasked regions greater than this length. (overrides partialOrderAlignmentMaskFilter in config)")
    parser.add_argument("--outVG", action="store_true", help = "export pangenome graph in VG (.vg) in addition to HAL")
    parser.add_argument("--outGFA", action="store_true", help = "export pangenome grpah in GFA (.gfa.gz) in addition to HAL")
    parser.add_argument("--batch", action="store_true", help = "Launch batch of alignments.  Input seqfile is expected to be chromfile as generated by cactus-graphmap-slit")
    parser.add_argument("--stagger", type=int, help = "Stagger alignment jobs in batch mode by this many seconds (to avoid starting all at once)", default=0)
    parser.add_argument("--acyclic", type=str, help = "Ensure that given genome is acyclic by deleting all paralogy edges in postprocessing")
    
    #Progressive Cactus Options
    parser.add_argument("--configFile", dest="configFile",
                        help="Specify cactus configuration file",
                        default=os.path.join(cactusRootPath(), "cactus_progressive_config.xml"))
    parser.add_argument("--root", dest="root", help="Name of ancestral node (which"
                        " must appear in NEWICK tree in <seqfile>) to use as a "
                        "root for the alignment.  Any genomes not below this node "
                        "in the tree may be used as outgroups but will never appear"
                        " in the output.  If no root is specifed then the root"
                        " of the tree is used. ", default=None)
    parser.add_argument("--latest", dest="latest", action="store_true",
                        help="Use the latest version of the docker container "
                        "rather than pulling one matching this version of cactus")
    parser.add_argument("--containerImage", dest="containerImage", default=None,
                        help="Use the the specified pre-built containter image "
                        "rather than pulling one from quay.io")
    parser.add_argument("--binariesMode", choices=["docker", "local", "singularity"],
                        help="The way to run the Cactus binaries", default=None)
    parser.add_argument("--nonCactusInput", action="store_true",
                        help="Input lastz cigars do not come from cactus-blast or cactus-refmap: Prepend ids in cigars")
    parser.add_argument("--database", choices=["kyoto_tycoon", "redis"],
                        help="The type of database", default="kyoto_tycoon")

    options = parser.parse_args()

    setupBinaries(options)
    setLoggingFromOptions(options)
    enableDumpStack()

    if (options.pathOverrides or options.pathOverrideNames):
        if not options.pathOverrides or not options.pathOverrideNames or \
           len(options.pathOverrideNames) != len(options.pathOverrides):
            raise RuntimeError('same number of values must be passed to --pathOverrides and --pathOverrideNames')

    # cactus doesn't run with 1 core
    if options.batchSystem == 'singleMachine':
        if options.maxCores is not None:
            if int(options.maxCores) < 2:
                raise RuntimeError('Cactus requires --maxCores > 1')
        else:
            # is there a way to get this out of Toil?  That would be more consistent
            if cpu_count() < 2:
                raise RuntimeError('Only 1 CPU detected.  Cactus requires at least 2')

    options.buildHal = True
    options.buildFasta = True

    if options.outHal.startswith('s3://'):
        if not has_s3:
            raise RuntimeError("S3 support requires toil to be installed with [aws]")
        # write a little something to the bucket now to catch any glaring problems asap
        test_file = os.path.join(getTempDirectory(), 'check')
        with open(test_file, 'w') as test_o:
                test_o.write("\n")
        region = get_aws_region(options.jobStore) if options.jobStore.startswith('aws:') else None
        write_s3(test_file, options.outHal if options.outHal.endswith('.hal') else os.path.join(options.outHal, 'test'), region=region)
        options.checkpointInfo = (get_aws_region(options.jobStore), options.outHal)
    else:
        options.checkpointInfo = None
        
    if options.batch:
        # the output hal is a directory, make sure it's there
        if not os.path.isdir(options.outHal):
            os.makedirs(options.outHal)
        assert len(options.cigarsFile) == 0
    else:
        assert len(options.cigarsFile) > 0

    # Mess with some toil options to create useful defaults.
    cactus_override_toil_options(options)

    # We set which type of unique ids to expect.  Numeric (from cactus-blast) or Eventname (cactus-refmap or cactus-grpahmap)
    # This is a bit ugly, since we don't have a good way to differentiate refmap from blast, and use --pangenome as a proxy
    # But I don't think there's a real use case yet of making a separate parameter
    options.eventNameAsID = os.environ.get('CACTUS_EVENT_NAME_AS_UNIQUE_ID')
    if options.eventNameAsID is not None:
        options.eventNameAsID = False if not bool(eventName) or eventName == '0' else True
    else:
        options.eventNameAsID = options.pangenome or options.pafInput
    os.environ['CACTUS_EVENT_NAME_AS_UNIQUE_ID'] = str(int(options.eventNameAsID))

    start_time = timeit.default_timer()
    with Toil(options) as toil:
        importSingularityImage(options)
        if options.restart:
            results_dict = toil.restart()
        else:
            align_jobs = make_batch_align_jobs(options, toil)
            results_dict = toil.start(Job.wrapJobFn(run_batch_align_jobs, align_jobs))

        # when using s3 output urls, things get checkpointed as they're made so no reason to export
        # todo: make a more unified interface throughout cactus for this
        # (see toil-vg's outstore logic which, while not perfect, would be an improvement
        if not options.outHal.startswith('s3://'):
            if options.batch:
                for chrom, results in results_dict.items():
                    toil.exportFile(results[0], makeURL(os.path.join(options.outHal, '{}.hal'.format(chrom))))
                    if options.outVG:
                        toil.exportFile(results[1], makeURL(os.path.join(options.outHal, '{}.vg'.format(chrom))))
                    if options.outGFA:
                        toil.exportFile(results[2], makeURL(os.path.join(options.outHal, '{}.gfa.gz'.format(chrom))))                    
            else:
                assert len(results_dict) == 1 and None in results_dict
                halID, vgID, gfaID = results_dict[None][0], results_dict[None][1], results_dict[None][2]
                # export the hal
                toil.exportFile(halID, makeURL(options.outHal))
                # export the vg
                if options.outVG:
                    toil.exportFile(vgID, makeURL(os.path.splitext(options.outHal)[0] + '.vg'))
                if options.outGFA:
                    toil.exportFile(gfaID, makeURL(os.path.splitext(options.outHal)[0] + '.gfa.gz'))
                                
    end_time = timeit.default_timer()
    run_time = end_time - start_time
    logger.info("cactus-align has finished after {} seconds".format(run_time))
    
def run_batch_align_jobs(job, jobs_dict):
    """ todo: clean this up """
    rv_dict = {}
    for chrom, chrom_job in jobs_dict.items():
        rv_dict[chrom] = job.addChild(chrom_job).rv()
    return rv_dict

def make_batch_align_jobs(options, toil):
    """ Make a dicitonary of align jobs """

    stagger_delay = 0
    result_dict = {}    
    if options.batch is True:
        #read the chrom file
        with open(options.seqFile, 'r') as chrom_file:
            for line in chrom_file:
                toks = line.strip().split()
                if len(toks):
                    assert len(toks) == 3
                    chrom, seqfile, alnFile = toks[0], toks[1], toks[2]
                    chrom_options = copy.deepcopy(options)
                    chrom_options.batch = False
                    chrom_options.seqFile = seqfile
                    chrom_options.cigarsFile = [alnFile]
                    chrom_options.stagger = stagger_delay
                    if chrom_options.checkpointInfo:
                        chrom_options.checkpointInfo = (chrom_options.checkpointInfo[0],
                                                        os.path.join(chrom_options.checkpointInfo[1], chrom + '.hal'))
                    chrom_align_job = make_align_job(chrom_options, toil)
                    result_dict[chrom] = chrom_align_job
                    stagger_delay += options.stagger
    else:
        result_dict[None] = make_align_job(options, toil)

    return result_dict
    
    
def make_align_job(options, toil):
    options.cactusDir = getTempDirectory()

    # apply path overrides.  this was necessary for wdl which doesn't take kindly to
    # text files of local paths (ie seqfile).  one way to fix would be to add support
    # for s3 paths and force wdl to use it.  a better way would be a more fundamental
    # interface shift away from files of paths throughout all of cactus
    if options.pathOverrides:
        seqFile = SeqFile(options.seqFile)
        configNode = ET.parse(options.configFile).getroot()
        config = ConfigWrapper(configNode)
        tree = MultiCactusTree(seqFile.tree)
        tree.nameUnlabeledInternalNodes(prefix = config.getDefaultInternalNodePrefix())                
        for name, override in zip(options.pathOverrideNames, options.pathOverrides):
            seqFile.pathMap[name] = override
        override_seq = os.path.join(options.cactusDir, 'seqFile.override')
        with open(override_seq, 'w') as out_sf:
            out_sf.write(str(seqFile))
        options.seqFile = override_seq

    if not options.root:
        seqFile = SeqFile(options.seqFile)
        configNode = ET.parse(options.configFile).getroot()
        config = ConfigWrapper(configNode)
        mcTree = MultiCactusTree(seqFile.tree)
        mcTree.nameUnlabeledInternalNodes(prefix=config.getDefaultInternalNodePrefix())
        options.root = mcTree.getRootName()

    if options.acyclic:
        seqFile = SeqFile(options.seqFile)
        tree = MultiCactusTree(seqFile.tree)
        leaves = [tree.getName(leaf) for leaf in tree.getLeaves()]
        if options.acyclic not in leaves:
            raise RuntimeError("Genome specified with --acyclic, {}, not found in tree leaves".format(options.acyclic))

    #to be consistent with all-in-one cactus, we make sure the project
    #isn't limiting itself to the subtree (todo: parameterize so root can
    #be passed through from prepare to blast/align)
    proj_options = copy.deepcopy(options)
    proj_options.root = None
    #Create the progressive cactus project (as we do in runCactusProgressive)
    projWrapper = ProjectWrapper(proj_options, proj_options.configFile, ignoreSeqPaths=options.root)
    projWrapper.writeXml()

    pjPath = os.path.join(options.cactusDir, ProjectWrapper.alignmentDirName,
                          '%s_project.xml' % ProjectWrapper.alignmentDirName)
    assert os.path.exists(pjPath)

    project = MultiCactusProject()

    if not os.path.isdir(options.cactusDir):
        os.makedirs(options.cactusDir)

    project.readXML(pjPath)

    # open up the experiment (as we do in ProgressiveUp.run)
    # note that we copy the path into the options here
    experimentFile = project.expMap[options.root]
    expXml = ET.parse(experimentFile).getroot()
    experiment = ExperimentWrapper(expXml)
    configPath = experiment.getConfigPath()
    configXml = ET.parse(configPath).getroot()

    seqIDMap = dict()
    tree = MultiCactusTree(experiment.getTree()).extractSubTree(options.root)
    leaves = [tree.getName(leaf) for leaf in tree.getLeaves()]
    outgroups = experiment.getOutgroupGenomes()
    genome_set = set(leaves + outgroups)

    # this is a hack to allow specifying all the input on the command line, rather than using suffix lookups
    def get_input_path(suffix=''):
        base_path = options.cigarsFile[0]
        for input_path in options.cigarsFile:
            if suffix and input_path.endswith(suffix):
                return input_path
            if os.path.basename(base_path).startswith(os.path.basename(input_path)):
                base_path = input_path
        return base_path + suffix

    # import the outgroups
    outgroupIDs = []
    outgroup_fragment_found = False
    for i, outgroup in enumerate(outgroups):
        try:
            outgroupID = toil.importFile(makeURL(get_input_path('.og_fragment_{}'.format(i))))
            outgroupIDs.append(outgroupID)
            experiment.setSequenceID(outgroup, outgroupID)
            outgroup_fragment_found = True
            assert not options.pangenome
        except:
            # we assume that input is not coming from cactus blast, so we'll treat output
            # sequences normally and not go looking for fragments
            outgroupIDs = []
            break

    #import the sequences (that we need to align for the given event, ie leaves and outgroups)
    for genome, seq in list(project.inputSequenceMap.items()):
        if genome in leaves or (not outgroup_fragment_found and genome in outgroups):
            if os.path.isdir(seq):
                tmpSeq = getTempFile()
                catFiles([os.path.join(seq, subSeq) for subSeq in os.listdir(seq)], tmpSeq)
                seq = tmpSeq
            seq = makeURL(seq)

            logger.info("Importing {}".format(seq))
            experiment.setSequenceID(genome, toil.importFile(seq))

    if not outgroup_fragment_found:
        outgroupIDs = [experiment.getSequenceID(outgroup) for outgroup in outgroups]

    # write back the experiment, as CactusWorkflowArguments wants a path
    experiment.writeXML(experimentFile)

    #import cactus config
    if options.configFile:
        cactusConfigID = toil.importFile(makeURL(options.configFile))
    else:
        cactusConfigID = toil.importFile(makeURL(project.getConfigPath()))
    project.setConfigID(cactusConfigID)

    project.syncToFileStore(toil)
    configNode = ET.parse(project.getConfigPath()).getroot()
    configWrapper = ConfigWrapper(configNode)
    configWrapper.substituteAllPredefinedConstantsWithLiterals()

    if options.singleCopySpecies:
        findRequiredNode(configWrapper.xmlRoot, "caf").attrib["alignmentFilter"] = "singleCopyEvent:{}".format(options.singleCopySpecies)

    if options.barMaskFilter:
        findRequiredNode(configWrapper.xmlRoot, "bar").attrib["partialOrderAlignmentMaskFilter"] = str(options.barMaskFilter)

    if options.pangenome:
        # turn off the megablock filter as it ruins non-all-to-all alignments
        findRequiredNode(configWrapper.xmlRoot, "caf").attrib["minimumBlockHomologySupport"] = "0"
        findRequiredNode(configWrapper.xmlRoot, "caf").attrib["minimumBlockDegreeToCheckSupport"] = "9999999999"
        # turn off mapq filtering
        findRequiredNode(configWrapper.xmlRoot, "caf").attrib["runMapQFiltering"] = "0"
        # more iterations here helps quite a bit to reduce underalignment
        findRequiredNode(configWrapper.xmlRoot, "caf").attrib["maxRecoverableChainsIterations"] = "50"                
        # turn down minimum block degree to get a fat ancestor
        findRequiredNode(configWrapper.xmlRoot, "bar").attrib["minimumBlockDegree"] = "1"
        # turn on POA
        findRequiredNode(configWrapper.xmlRoot, "bar").attrib["partialOrderAlignment"] = "1"
        # save it
        if not options.batch:
            pg_file = options.outHal + ".pg-conf.xml"
            if pg_file.startswith('s3://'):
                pg_temp_file = getTempFile()
            else:
                pg_temp_file = pg_file            
            configWrapper.writeXML(pg_temp_file)
            if pg_file.startswith('s3://'):
                write_s3(pg_temp_file, pg_file, region=get_aws_region(options.jobStore))
            logger.info("pangenome configuration overrides saved in {}".format(pg_file))

    workFlowArgs = CactusWorkflowArguments(options, experimentFile=experimentFile, configNode=configNode, seqIDMap = project.inputSequenceIDMap)

    #import the files that cactus-blast made
    workFlowArgs.alignmentsID = toil.importFile(makeURL(get_input_path()))
    workFlowArgs.secondaryAlignmentsID = None
    if not options.pafInput:
        try:
            workFlowArgs.secondaryAlignmentsID = toil.importFile(makeURL(get_input_path('.secondary')))
        except:
            pass
    workFlowArgs.outgroupFragmentIDs = outgroupIDs
    workFlowArgs.ingroupCoverageIDs = []
    if outgroup_fragment_found and len(outgroups) > 0:
        for i in range(len(leaves)):
            workFlowArgs.ingroupCoverageIDs.append(toil.importFile(makeURL(get_input_path('.ig_coverage_{}'.format(i)))))

    align_job = Job.wrapJobFn(run_cactus_align,
                              configWrapper,
                              workFlowArgs,
                              project,
                              checkpointInfo=options.checkpointInfo,
                              doRenaming=options.nonCactusInput,
                              pafInput=options.pafInput,
                              pafSecondaries=options.usePafSecondaries,
                              doVG=options.outVG,
                              doGFA=options.outGFA,
                              delay=options.stagger,
                              eventNameAsID=options.eventNameAsID,
                              acyclicEvent=options.acyclic)
    return align_job

def run_cactus_align(job, configWrapper, cactusWorkflowArguments, project, checkpointInfo, doRenaming, pafInput, pafSecondaries, doVG, doGFA, delay=0, eventNameAsID=False, acyclicEvent=None):
    # this option (--stagger) can be used in batch mode to avoid starting all the alignment jobs at the same time
    time.sleep(delay)
    
    head_job = Job()
    job.addChild(head_job)

    # allow for input in paf format:
    if pafInput:
        # convert the paf input to lastz format, splitting out into primary and secondary files
        paf_to_lastz_job = head_job.addChildJobFn(paf_to_lastz.paf_to_lastz, cactusWorkflowArguments.alignmentsID, True)
        cactusWorkflowArguments.alignmentsID = paf_to_lastz_job.rv(0)
        cactusWorkflowArguments.secondaryAlignmentsID = paf_to_lastz_job.rv(1) if pafSecondaries else None

    # do the name mangling cactus expects, where every fasta sequence starts with id=0|, id=1| etc
    # and the cigar files match up.  If reading cactus-blast output, the cigars are fine, just need
    # the fastas (todo: make this less hacky somehow)
    cur_job = head_job.addFollowOnJobFn(run_prepend_unique_ids, cactusWorkflowArguments, project, doRenaming, eventNameAsID,
                                        #todo disk=
    )
    no_ingroup_coverage = not cactusWorkflowArguments.ingroupCoverageIDs
    cactusWorkflowArguments = cur_job.rv()
    
    if no_ingroup_coverage:
        # if we're not taking cactus_blast input, then we need to recompute the ingroup coverage
        cur_job = cur_job.addFollowOnJobFn(run_ingroup_coverage, cactusWorkflowArguments, project)
        cactusWorkflowArguments = cur_job.rv()

    # run cactus setup all the way through cactus2hal generation
    setup_job = cur_job.addFollowOnJobFn(run_setup_phase, cactusWorkflowArguments)

    # set up the project
    prepare_hal_export_job = setup_job.addFollowOnJobFn(run_prepare_hal_export, project, setup_job.rv())

    # create the hal
    hal_export_job = prepare_hal_export_job.addFollowOnJobFn(exportHal, prepare_hal_export_job.rv(0), event=prepare_hal_export_job.rv(1),
                                                             checkpointInfo=checkpointInfo, acyclicEvent=acyclicEvent,
                                                             memory=configWrapper.getDefaultMemory(),
                                                             disk=configWrapper.getExportHalDisk(),
                                                             preemptable=False)

    # optionally create the VG
    if doVG or doGFA:
        vg_export_job = hal_export_job.addFollowOnJobFn(export_vg, hal_export_job.rv(), configWrapper, doVG, doGFA,
                                                        checkpointInfo=checkpointInfo)
        vg_file_id, gfa_file_id = vg_export_job.rv(0), vg_export_job.rv(1)
    else:
        vg_file_id, gfa_file_id = None, None
        
    return hal_export_job.rv(), vg_file_id, gfa_file_id

def prepend_cigar_ids(cigars, outputDir, idMap):
    """ like cactus_workflow.prependUniqueIDs, but runs on cigar files.  requires name map
    updated by prependUniqueIDs """
    ret = []
    for cigar in cigars:
        outPath = os.path.join(outputDir, os.path.basename(cigar))
        with open(outPath, 'w') as outfile, open(cigar, 'r') as infile:
            for line in infile:
                toks = line.split()
                if toks[1] not in idMap:
                    raise RuntimeError('cigar id {} not found in id-map {}'.format(toks[1], str(idMap)[:1000]))
                if toks[5] not in idMap:
                    raise RuntimeError('cigar id {} not found in id-map {}'.format(toks[5], str(idMap)[:1000]))
                toks[1] = idMap[toks[1]]
                toks[5] = idMap[toks[5]]
                outfile.write('{}\n'.format(' '.join(toks)))
        ret.append(outPath)
    return ret

def run_prepend_unique_ids(job, cactusWorkflowArguments, project, renameCigars, eventNameAsID):
    """ prepend the unique ids on the input fasta.  this is required for cactus to work (would be great to relax it though"""

    # note, there is an order dependence to everything where we have to match what was done in cactus_workflow
    # (so the code is pasted exactly as it is there)
    # this is horrible and needs to be fixed via drastic interface refactor
    # update: this has been somewhat fixed with a minor refactor: prependUniqueIDs is no longer order dependent (but takes dict instead of list)
    exp = cactusWorkflowArguments.experimentWrapper
    ingroupsAndOriginalIDs = [(g, exp.getSequenceID(g)) for g in exp.getGenomesWithSequence() if g not in exp.getOutgroupGenomes()]
    eventToSequence = {}
    for g, seqID in ingroupsAndOriginalIDs:
        seqPath = job.fileStore.getLocalTempFile() + '.fa'
        if project.inputSequenceMap[g].endswith('.gz'):
            seqPath += '.gz'
        job.fileStore.readGlobalFile(seqID, seqPath)
        if seqPath.endswith('.gz'):
            cactus_call(parameters=['gzip', '-d', '-c', seqPath], outfile=seqPath[:-3])
            seqPath = seqPath[:-3]
        eventToSequence[g] = seqPath
    cactusWorkflowArguments.totalSequenceSize = sum(os.stat(x).st_size for x in eventToSequence.values())
    # need to have outgroups in there just for id naming (don't need their sequence)
    for g in exp.getOutgroupGenomes():
        eventToSequence[g] = None
    renamedInputSeqDir = job.fileStore.getLocalTempDir()
    id_map = {}
    eventToUnique = prependUniqueIDs(eventToSequence, renamedInputSeqDir, idMap=id_map, eventNameAsID=eventNameAsID)
    # Set the uniquified IDs for the ingroups and outgroups
    for event, uniqueFa in eventToUnique.items():
        uniqueFaID = job.fileStore.writeGlobalFile(uniqueFa, cleanup=True)
        cactusWorkflowArguments.experimentWrapper.setSequenceID(event, uniqueFaID)

    # if we're not taking cactus-[blast|refmap] input, then we have to apply to the cigar files too
    if renameCigars:
        alignments = job.fileStore.readGlobalFile(cactusWorkflowArguments.alignmentsID)
        renamed_alignments = prepend_cigar_ids([alignments], renamedInputSeqDir, id_map)
        cactusWorkflowArguments.alignmentsID = job.fileStore.writeGlobalFile(renamed_alignments[0], cleanup=True)
        if cactusWorkflowArguments.secondaryAlignmentsID:
            sec_alignments = job.fileStore.readGlobalFile(cactusWorkflowArguments.secondaryAlignmentsID)
            renamed_sec_alignments = prepend_cigar_ids([sec_alignments], renamedInputSeqDir, id_map)
            cactusWorkflowArguments.secondaryAlignmentsID = job.fileStore.writeGlobalFile(renamed_sec_alignments[0], cleanup=True)
        if cactusWorkflowArguments.outgroupFragmentIDs:
            og_alignments= job.fileStore.readGlobalFile(cactusWorkflowArguments.outgroupFragmentIDs)
            renamed_og_alignments = prepend_cigar_ids(og_alignments, renamedInputSeqDir, id_map)
            cactusWorkflowArguments.outgroupFragmentIDs = [job.fileStore.writeGlobalFile(rga, cleanup=True) for rga in renamed_og_alignments]
    
    return cactusWorkflowArguments

def run_ingroup_coverage(job, cactusWorkflowArguments, project):
    """ for every ingroup genome, make a bed file by computing its coverge vs the outgroups """
    work_dir=job.fileStore.getLocalTempDir()
    exp = cactusWorkflowArguments.experimentWrapper
    ingroupsAndOriginalIDs = [(g, exp.getSequenceID(g)) for g in exp.getGenomesWithSequence() if g not in exp.getOutgroupGenomes()]
    outgroups = [job.fileStore.readGlobalFile(id) for id in cactusWorkflowArguments.outgroupFragmentIDs]
    sequences = [job.fileStore.readGlobalFile(id) for id in map(itemgetter(1), ingroupsAndOriginalIDs)]
    cactusWorkflowArguments.totalSequenceSize = sum(os.stat(x).st_size for x in sequences)
    ingroups = map(itemgetter(0), ingroupsAndOriginalIDs)
    cigar = job.fileStore.readGlobalFile(cactusWorkflowArguments.alignmentsID)
    if len(outgroups) > 0:
        # should we parallelize with child jobs?
        for ingroup, sequence in zip(ingroups, sequences):
            coverage_path = os.path.join(work_dir, '{}.coverage'.format(sequence))
            calculateCoverage(sequence, cigar, coverage_path, fromGenome=outgroups, work_dir=work_dir)
            cactusWorkflowArguments.ingroupCoverageIDs.append(job.fileStore.writeGlobalFile(coverage_path))
    return cactusWorkflowArguments

def run_setup_phase(job, cactusWorkflowArguments):
    # needs to be its own job to resovolve the workflowargument promise
    return job.addChild(CactusSetupCheckpoint(cactusWorkflowArguments=cactusWorkflowArguments, phaseName="setup")).rv()

def run_prepare_hal_export(job, project, experiment):
    """ hack up the given project into something that gets exportHal() to do what we want """
    event = experiment.getRootGenome()
    exp_path = os.path.join(job.fileStore.getLocalTempDir(), event + '_experiment.xml')
    experiment.writeXML(exp_path)
    project.expMap = {event : experiment}
    project.expIDMap = {event : job.fileStore.writeGlobalFile(exp_path)}
    return project, event

def export_vg(job, hal_id, configWrapper, doVG, doGFA, checkpointInfo=None, resource_spec = False):
    """ use hal2vg to convert the HAL to vg format """

    if not resource_spec:
        # caller couldn't figure out the resrouces from hal_id promise.  do that
        # now and try again
        return job.addChildJobFn(export_vg, hal_id, configWrapper, doVG, doGFA, checkpointInfo,
                                 resource_spec = True,
                                 disk=hal_id.size * 3,
                                 memory=hal_id.size * 10).rv()
        
    work_dir = job.fileStore.getLocalTempDir()
    hal_path = os.path.join(work_dir, "out.hal")
    job.fileStore.readGlobalFile(hal_id, hal_path)
    
    graph_event = getOptionalAttrib(findRequiredNode(configWrapper.xmlRoot, "graphmap"), "assemblyName", default="_MINIGRAPH_")
    hal2vg_opts = getOptionalAttrib(findRequiredNode(configWrapper.xmlRoot, "hal2vg"), "hal2vgOptions", default="")
    if hal2vg_opts:
        hal2vg_opts = hal2vg_opts.split(' ')
    else:
        hal2vg_opts = []
    ignore_events = []
    if not getOptionalAttrib(findRequiredNode(configWrapper.xmlRoot, "hal2vg"), "includeMinigraph", typeFn=bool, default=False):
        ignore_events.append(graph_event)
    if not getOptionalAttrib(findRequiredNode(configWrapper.xmlRoot, "hal2vg"), "includeAncestor", typeFn=bool, default=False):
        ignore_events.append(configWrapper.getDefaultInternalNodePrefix() + '0')
    if ignore_events:
        hal2vg_opts += ['--ignoreGenomes', ','.join(ignore_events)]
    if not getOptionalAttrib(findRequiredNode(configWrapper.xmlRoot, "hal2vg"), "prependGenomeNames", typeFn=bool, default=True):
        hal2vg_opts += ['--onlySequenceNames']

    vg_path = os.path.join(work_dir, "out.vg")
    cmd = ['hal2vg', hal_path] + hal2vg_opts

    cactus_call(parameters=cmd, outfile=vg_path)

    if checkpointInfo:
        write_s3(vg_path, os.path.splitext(checkpointInfo[1])[0] + '.vg', region=checkpointInfo[0])

    gfa_path = os.path.join(work_dir, "out.gfa.gz")
    if doGFA:
        gfa_cmd = [ ['vg', 'view', '-g', vg_path], ['gzip'] ]
        cactus_call(parameters=gfa_cmd, outfile=gfa_path)

        if checkpointInfo:
            write_s3(gfa_path, os.path.splitext(checkpointInfo[1])[0] + '.gfa.gz', region=checkpointInfo[0])

    vg_id = job.fileStore.writeGlobalFile(vg_path) if doVG else None
    gfa_id = job.fileStore.writeGlobalFile(gfa_path) if doGFA else None

    return vg_id, gfa_id

def main_batch():
    """ this is a bit like cactus-align --batch except it will use toil-in-toil to assign each chromosome to a machine.
    pros: much less chance of a problem with one chromosome affecting anything else
          more forgiving for inexact resource specs
          could be ported to Terra
    cons: less efficient use of resources
    """
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    addCactusWorkflowOptions(parser)

    parser.add_argument("chromFile", help = "chroms file")
    parser.add_argument("outHal", type=str, help = "Output directory (can be s3://)")
    parser.add_argument("--alignOptions", type=str, help = "Options to pass through to cactus-align (don't forget to wrap in quotes)")
    parser.add_argument("--alignCores", type=int, help = "Number of cores per align job")
    parser.add_argument("--alignCoresOverrides", nargs="*", help = "Override align job cores for a chromosome. Space-separated list of chrom,cores pairse epxected")

    parser.add_argument("--configFile", dest="configFile",
                        help="Specify cactus configuration file",
                        default=os.path.join(cactusRootPath(), "cactus_progressive_config.xml"))

    options = parser.parse_args()

    options.containerImage=None
    options.binariesMode=None
    options.root=None
    options.latest=None
    options.database="kyoto_tycoon"

    setupBinaries(options)
    setLoggingFromOptions(options)
    enableDumpStack()

    # Mess with some toil options to create useful defaults.
    cactus_override_toil_options(options)

    # Turn the overrides into a dict
    cores_overrides = {}
    if options.alignCoresOverrides:
        for o in options.alignCoresOverrides:
            try:
                chrom, cores = o.split(',')
                cores_overrides[chrom] = int(cores)
            except:
                raise RuntimeError("Error parsing alignCoresOverrides \"{}\"".format(o))
    options.alignCoresOverrides = cores_overrides                

    start_time = timeit.default_timer()
    with Toil(options) as toil:
        importSingularityImage(options)
        if options.restart:
            results_dict = toil.restart()
        else:
            config_id = toil.importFile(makeURL(options.configFile))
            # load the chromfile into memory
            chrom_dict = {}
            with open(options.chromFile, 'r') as chrom_file:
                for line in chrom_file:
                    toks = line.strip().split()
                    if len(toks):
                        assert len(toks) == 3
                        chrom, seqfile, alnFile = toks[0], toks[1], toks[2]
                        chrom_dict[chrom] = toil.importFile(makeURL(seqfile)), toil.importFile(makeURL(alnFile))
            results_dict = toil.start(Job.wrapJobFn(align_toil_batch, chrom_dict, config_id, options))

        # when using s3 output urls, things get checkpointed as they're made so no reason to export
        # todo: make a more unified interface throughout cactus for this
        # (see toil-vg's outstore logic which, while not perfect, would be an improvement
        if not options.outHal.startswith('s3://'):
            if options.batch:
                for chrom, results in results_dict.items():
                    toil.exportFile(results[0], makeURL(os.path.join(options.outHal, '{}.hal'.format(chrom))))
                    if options.outVG:
                        toil.exportFile(results[1], makeURL(os.path.join(options.outHal, '{}.vg'.format(chrom))))
                    if options.outGFA:
                        toil.exportFile(results[2], makeURL(os.path.join(options.outHal, '{}.gfa.gz'.format(chrom))))
                    toil.exportFile(results[3], makeURL(os.path.join(options.outHal, '{}.hal.log'.format(chrom))))
                                
    end_time = timeit.default_timer()
    run_time = end_time - start_time
    logger.info("cactus-align-batch has finished after {} seconds".format(run_time))

def align_toil_batch(job, chrom_dict, config_id, options):
    """ spawn a toil job for each cactus-align """

    results_dict = {}
    for chrom in chrom_dict.keys():
        seq_file_id, paf_file_id = chrom_dict[chrom]
        align_job = job.addChildJobFn(align_toil, chrom, seq_file_id, paf_file_id, config_id, options,
                                      cores=options.alignCoresOverrides[chrom] if chrom in options.alignCoresOverrides else options.alignCores)
        results_dict[chrom] = align_job.rv()

    return results_dict

def align_toil(job, chrom, seq_file_id, paf_file_id, config_id, options):
    """ run cactus-align """
    
    work_dir = job.fileStore.getLocalTempDir()
    config_file = os.path.join(work_dir, 'config.xml')
    job.fileStore.readGlobalFile(config_id, config_file)

    seq_file = os.path.join(work_dir, '{}_seq_file.txt'.format(chrom))
    job.fileStore.readGlobalFile(seq_file_id, seq_file)

    paf_file = os.path.join(work_dir, '{}.paf'.format(chrom))
    job.fileStore.readGlobalFile(paf_file_id, paf_file)

    js = os.path.join(work_dir, 'js')

    if options.outHal.startswith('s3://'):
        out_file = os.path.join(options.outHal, '{}.hal'.format(chrom))
    else:
        out_file = os.path.join(work_dir, '{}.hal'.format(chrom))

    log_file = os.path.join(work_dir, '{}.hal.log'.format(chrom))

    cmd = ['cactus-align', js, seq_file, paf_file, out_file, '--logFile', log_file] + options.alignOptions.split()

    cactus_call(parameters=cmd)

    ret_ids = [None, None, None, None]

    if not options.outHal.startswith('s3://'):
        # we're not checkpoint directly to s3, so we return 
        ret_ids[0] = job.fileStore.writeGlobalFile(out_file)
        out_vg = os.path.splitext(out_file)[0] + '.vg'
        if os.path.exists(out_vg):
            ret_ids[1] = job.fileStore.writeGlobalFile(out_vg)
        out_gfa = os.path.splitext(out_file)[0] + '.gfa.gz'
        if os.path.exists(out_gfa):
            ret_ids[2] = job.fileStore.writeGlobalFile(out_gfa)
        ret_ids[3] = job.fileStore.writeGlobalFile(log_file)
    else:
        write_s3(log_file, out_file + '.log', region=get_aws_region(options.jobStore))            

    return ret_ids

if __name__ == '__main__':
    main()
