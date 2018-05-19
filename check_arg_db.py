#!/usr/bin/env python3

from ArgditLib import CDSPredict
from ArgditLib.Config import Config
from ArgditLib import EntrezDBAccess
from ArgditLib import MultiSeqAlign
from ArgditLib import OptionParser
from ArgditLib import Translate
from ArgditLib import Utils
from ArgditLib.ProcLog import ProcLog
from ArgditLib.SequenceFileParser import SequenceFileParser
from Bio import AlignIO
from Bio import SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from functools import partial
from multiprocessing import Pool
import argparse
import os
import subprocess
import sys
import tempfile

'''ARG database validation main program'''

'''
Function name: export_refined_seqs
Inputs       : Refined sequences, input ARG database file path
Outputs      : Refined sequence file path
Description  : Export refined ARG sequences to a FASTA file in the same directory of the input database
               file
'''
def export_refined_seqs(refined_seq_records, seq_db_path):
    output_seq_file_path = Utils.create_supp_file_path(seq_db_path, '_auto_refine.fa')
    with open(output_seq_file_path, 'w') as f:
        for seq_record in refined_seq_records:
            SeqIO.write(seq_record, f, 'fasta')

    return output_seq_file_path

'''
Function name: search_outlier_seqs_core
Inputs       : Sequences of one ARG ontology class, minimum number of sequences requirement, bootstrap
               factor, bootstrap iterations
Outputs      : Detected outlier sequences
Description  : Core function to perform outlier sequence detection using OD-seq
'''
def search_outlier_seqs_core(class_seq_record_tuple, min_seq_count, bootstrap_factor, bootstrap_iter = None):
    seq_records = class_seq_record_tuple[1]

    if len(seq_records) < min_seq_count:
        return []

    '''
    Perform multiple sequence alignment using MUSCLE, with the alignment results written to a temporary
    file
    '''
    multi_seq_align = MultiSeqAlign.muscleAlign(seq_records)
    with tempfile.NamedTemporaryFile(mode = 'w', delete = False) as f:
        AlignIO.write(multi_seq_align, f, 'fasta')
        tmp_align_file_path = f.name

    with tempfile.NamedTemporaryFile(delete = False) as f:
        tmp_outlier_file_path = f.name

    '''When bootstrap iteration not specified, bootstrap iteration = no. of sequences x bootstrap factor'''
    if bootstrap_iter is None:
        bootstrap_iter = str(len(seq_records) * bootstrap_factor)

    '''Perform outlier detection using OD-seq, with the outlier sequences exported to a temporary file'''
    subprocess_args = ['OD-seq', '-i', tmp_align_file_path, '-f', 'fa', '-o', tmp_outlier_file_path, '--full',
                       '-t', '1', '--boot-rep', bootstrap_iter]
    child_process = subprocess.run(subprocess_args,
                                   stdin = subprocess.DEVNULL,
                                   stdout = subprocess.DEVNULL,
                                   stderr = subprocess.DEVNULL,
                                   universal_newlines = True)

    outlier_seq_records = list()
    with open(tmp_outlier_file_path, 'r') as f:
        for outlier_seq_record in SeqIO.parse(f, 'fasta'):
            outlier_seq_records.append(outlier_seq_record)

    os.remove(tmp_align_file_path)
    os.remove(tmp_outlier_file_path)

    return outlier_seq_records

'''Entry point of the main program'''
parser = argparse.ArgumentParser()
parser.add_argument('seq_db_path', help = 'nucleotide/protein database FASTA file path')
parser.add_argument('-f', '--fields', action = 'store', dest = 'otl_label_field_num_opt',
                    help = 'ontology label field numbers for ontology class outlier sequence detection')
parser.add_argument('-r', '--refine', action = 'store_true', help = 'export refined DNA sequences')
parser.add_argument('-e', '--exportlog', action = 'store_true', help = 'export validation results and process log')
args = parser.parse_args()

otl_label_field_nums = None
is_check_otl_annot = False
ProcLog.init_logs()

config = Config('config.ini')

if not os.path.exists(args.seq_db_path):
    ProcLog.log_exec_error('Database file \'{}\' does not exist'.format(args.seq_db_path))

if args.otl_label_field_num_opt is not None:
    otl_label_field_nums = OptionParser.parse_ontology_label_field_nums(args.otl_label_field_num_opt)
    is_check_otl_annot = True

if ProcLog.has_exec_error():
    ProcLog.export_exec_error(sys.stdout)
    sys.exit()

search_outlier_seqs = partial(search_outlier_seqs_core, min_seq_count = config.min_seq_count,
                              bootstrap_factor = config.bootstrap_factor)

seq_file_parser = SequenceFileParser()
seq_file_parser.parse(args.seq_db_path)

'''
Log sequences with invalid NCBI accession number format, unknown sequence type, duplicated headers,
and redundant sequences
'''
for seq_record_id in seq_file_parser.get_invalid_acc_num_fmt_seq_rec_ids():
    ProcLog.log_invalid_acc_num_fmt(msg = seq_record_id)

for seq_record_id in seq_file_parser.get_unknown_seq_type_seq_rec_ids():
    ProcLog.log_unknown_seq_type(msg = seq_record_id)

for seq_record_id in seq_file_parser.get_duplicated_headers():
    ProcLog.log_duplicated_header(seq_record_id)

for seq_record_id, redundant_seq_record_id, is_rev_comp in seq_file_parser.get_redundant_seq_pairs():
    ProcLog.log_redundant_seq(seq_record_id, redundant_seq_record_id, is_rev_comp)

EntrezDBAccess.set_entrez_email(config.entrez_email)

'''
For ARG nucleotide and protein sequences with NCBI nucleotide accession numbers, predict the lengths of
their potential CDS sequences, and use these lengths as sequence length filters to select target CDS
regions; the predicted CDS sequences are also stored for ARG nucleotide sequences
'''
nt_id_nt_seq_records = seq_file_parser.get_nt_id_nt_seq_records()
nt_id_protein_seq_records = seq_file_parser.get_nt_id_protein_seq_records()
cds_seq_len_filters, candidate_cds_seq_segment_map = CDSPredict.predict_cds_regions(nt_id_nt_seq_records,
                                                                                    nt_id_protein_seq_records)

query_protein_acc_nums = set()

if len(cds_seq_len_filters) > 0:
    print('Retrieving information from NCBI nucleotide database...')
    '''
    Target CDS regions contain potential CDS sequence matches, i.e. an ARG nucleotide/protein sequence
    should come from one of the relevant target CDS regions
    '''
    target_cds_region_grps, target_cds_protein_acc_nums, is_parse_complete = \
        EntrezDBAccess.search_target_cds_by_nt_acc_num(cds_seq_len_filters.keys(), cds_seq_len_filters)

    if not is_parse_complete:
        ProcLog.log_data_retrieval_error()

    if ProcLog.has_exec_error():
        ProcLog.export_exec_error(sys.stdout)
        sys.exit()

    '''Add the protein accession numbers of the target CDS regions to the NCBI protein query'''
    query_protein_acc_nums.update(target_cds_protein_acc_nums)

    '''Keep the latest nucleotide accession version to determine potential obsolete sequences'''
    latest_ver_nt_acc_num_map = dict()
    for nt_acc_num in target_cds_region_grps.keys():
        latest_ver_nt_acc_num_map[Utils.trim_version(nt_acc_num)] = nt_acc_num

    '''Store (non-versioned) nucleotide accession numbers of the target CDS regions'''
    matched_nt_non_ver_acc_nums = latest_ver_nt_acc_num_map.keys()

'''Add the protein accession numbers of the ARG sequences to the NCBI protein query'''
protein_id_nt_seq_records = seq_file_parser.get_protein_id_nt_seq_records()
if len(protein_id_nt_seq_records) > 0:
    query_protein_acc_nums.update(map(Utils.trim_version, protein_id_nt_seq_records.keys()))

protein_id_protein_seq_records = seq_file_parser.get_protein_id_protein_seq_records()
if len(protein_id_protein_seq_records) > 0:
    query_protein_acc_nums.update(map(Utils.trim_version, protein_id_protein_seq_records.keys()))

print('Retrieving information from NCBI protein database...')
genbank_protein_info_set = EntrezDBAccess.search_protein_info(query_protein_acc_nums)

if ProcLog.has_exec_error():
    ProcLog.export_exec_error(sys.stdout)
    sys.exit()

'''
It is possible that some retrieved records are identifed by non-accession format identifiers, hence
they need to be extracted for special matching
'''
non_acc_fmt_protein_ids = set(genbank_protein_info_set.keys()) - query_protein_acc_nums

'''Keep the latest protein accession version to determine potential obsolete sequences'''
latest_ver_protein_acc_num_map = dict()
for protein_acc_num in genbank_protein_info_set.keys():
    latest_ver_protein_acc_num_map[Utils.trim_version(protein_acc_num)] = protein_acc_num

'''Store (non-versioned) protein accession numbers of the target CDS regions'''
matched_protein_non_ver_acc_nums = latest_ver_protein_acc_num_map.keys()

'''Validate the sequences of the four categories'''
otl_protein_seq_record_grps = dict()
refined_nt_seq_records = list()
protein_id_mapping_msg_template = ProcLog.PROTEIN_ID_MAPPING_MSG_TEMPLATE

'''Validate ARG nucleotide sequences annotated by NCBI nucleotide accession numbers'''
for nt_acc_num, nt_seq_records in nt_id_nt_seq_records.items():
    nt_non_ver_acc_num = Utils.trim_version(nt_acc_num)

    '''
    Accession number not found either because this accession number does not exist, or no target CDS
    region can be found (i.e. no CDS region matches the predicted CDS sequence lengths)
    '''
    if nt_non_ver_acc_num not in matched_nt_non_ver_acc_nums:
        for nt_seq_record in nt_seq_records:
            ProcLog.log_acc_num_not_found(msg = nt_seq_record.id)
    
        continue

    latest_ver_nt_acc_num = latest_ver_nt_acc_num_map[nt_non_ver_acc_num]
    is_ver_obsolete = (nt_acc_num != latest_ver_nt_acc_num and not Utils.is_non_version_acc_num(nt_acc_num))

    for nt_seq_record in nt_seq_records:
        '''
        Try to translate the predicted CDS sequences using the target CDS regions, and then compare
        with the protein products specified in the target CDS regions. Matched protein is stored in
        translated_protein_info and matched_cds_seq_segment contains the correctly predicted CDS
        sequence
        '''
        translated_protein_info, matched_cds_seq_segment, _, non_acc_fmt_protein_id_mapping = \
            Translate.search_correct_cds_translation(candidate_cds_seq_segment_map[nt_seq_record.id],
                                                     target_cds_region_grps[latest_ver_nt_acc_num],
                                                     genbank_protein_info_set, non_acc_fmt_protein_ids)

        if translated_protein_info is None:
            ProcLog.log_seq_mismatch(nt_seq_record.id, is_ver_obsolete)
        else:
            if is_ver_obsolete:
                ProcLog.log_obsolete_ver(msg = nt_seq_record.id)

            if non_acc_fmt_protein_id_mapping is not None:
                ProcLog.log_exec_msg(protein_id_mapping_msg_template.format(nt_seq_record.id,
                                                                            non_acc_fmt_protein_id_mapping[0],
                                                                            non_acc_fmt_protein_id_mapping[1]))

            '''
            If the correctly predicted CDS sequence differs from the original ARG sequence in length,
            then extra nucleotides were preprended and/or appended to the true CDS sequence (at most 2
            nucleotides at each end), and the true CDS sequence is exported as refined sequence
            '''
            nt_seq_str = str(nt_seq_record.seq)
            if args.refine and len(nt_seq_str) != len(matched_cds_seq_segment.seq_str):
                refined_nt_seq_records.append(SeqRecord(Seq(matched_cds_seq_segment.seq_str),
                                                        id = nt_seq_record.description, name = '', description = ''))

            '''
            Group the protein product according to the ARG ontology class for subsequent outlier
            sequence detection
            '''
            if is_check_otl_annot:
                protein_seq_record = SeqRecord(Seq(translated_protein_info.seq_str), id = nt_seq_record.id,
                                               name = '', description = '')
                Utils.group_protein_by_otl_class(otl_protein_seq_record_grps, protein_seq_record,
                                                otl_label_field_nums, config)

'''Validate ARG protein sequences annotated by NCBI nucleotide accession numbers'''
for nt_acc_num, protein_seq_records in nt_id_protein_seq_records.items():
    nt_non_ver_acc_num = Utils.trim_version(nt_acc_num)

    '''
    Accession number not found either because this accession number does not exist, or no target CDS
    region can be found (i.e. no CDS region matches the predicted CDS sequence lengths)
    '''
    if nt_non_ver_acc_num not in matched_nt_non_ver_acc_nums:
        for protein_seq_record in protein_seq_records:
            ProcLog.log_acc_num_not_found(msg = protein_seq_record.id)

        continue

    latest_ver_nt_acc_num = latest_ver_nt_acc_num_map[nt_non_ver_acc_num]
    is_ver_obsolete = (nt_acc_num != latest_ver_nt_acc_num and not Utils.is_non_version_acc_num(nt_acc_num))

    for protein_seq_record in protein_seq_records:
        protein_seq_str = str(protein_seq_record.seq)
        is_protein_seq_matched = False

        '''
        Compare the ARG protein sequence with the protein products specified in the target CDS regions
        '''
        for target_cds_region in target_cds_region_grps[latest_ver_nt_acc_num]:
            if target_cds_region.protein_id in genbank_protein_info_set:
                genbank_protein_info = genbank_protein_info_set[target_cds_region.protein_id]
                if protein_seq_str == genbank_protein_info.seq_str:
                    is_protein_seq_matched = True
                    break
            else:
                '''
                Search the ARG protein sequence in protein products identified by non-accession format
                identifiers
                '''
                matched_protein_id = Utils.match_non_acc_fmt_genbank_protein_seqs(protein_seq_str,
                                                                                  genbank_protein_info_set,
                                                                                  non_acc_fmt_protein_ids)
                if matched_protein_id is not None:
                    ProcLog.log_exec_msg(protein_id_mapping_msg_template.format(protein_seq_record.id,
                                                                                target_cds_region.protein_id,
                                                                                matched_protein_id))
                    is_protein_seq_matched = True
                    break

        if is_protein_seq_matched:
            if is_ver_obsolete:
                ProcLog.log_obsolete_ver(msg = protein_seq_record.id)

            '''
            Group the ARG protein sequence according to the ARG ontology class for subsequent outlier
            sequence detection
            '''
            if is_check_otl_annot:
                Utils.group_protein_by_otl_class(otl_protein_seq_record_grps, protein_seq_record,
                                                otl_label_field_nums, config)
        else:
            ProcLog.log_seq_mismatch(protein_seq_record.id, is_ver_obsolete)

'''Validate ARG nucleotide sequences annotated by NCBI protein accession numbers'''
for protein_acc_num, nt_seq_records in protein_id_nt_seq_records.items():
    protein_non_ver_acc_num = Utils.trim_version(protein_acc_num)

    if protein_non_ver_acc_num in matched_protein_non_ver_acc_nums:
        latest_ver_protein_acc_num = latest_ver_protein_acc_num_map[protein_non_ver_acc_num]
        is_ver_obsolete = (protein_acc_num != latest_ver_protein_acc_num and \
                           not Utils.is_non_version_acc_num(protein_acc_num))
    else:
        latest_ver_protein_acc_num = None
        is_ver_obsolete = False
       
    for nt_seq_record in nt_seq_records:
        nt_seq_str = str(nt_seq_record.seq)
        '''Translate the ARG nucleotide sequence and obtain products from all six frames'''
        translated_protein_seq_strs = Translate.translate(nt_seq_str)

        if latest_ver_protein_acc_num is not None:
            genbank_protein_info = genbank_protein_info_set[latest_ver_protein_acc_num]
            is_protein_seq_matched = genbank_protein_info.seq_str in translated_protein_seq_strs
        else:
            '''
            Search the translation candidates in protein products identified by non-accession format
            identifiers
            '''
            matched_protein_id = Utils.match_non_acc_fmt_genbank_protein_seqs(translated_protein_seq_strs,
                                                                              genbank_protein_info_set,
                                                                              non_acc_fmt_protein_ids)
            if matched_protein_id is None:
                is_protein_seq_matched = False
            else:
                ProcLog.log_exec_msg(protein_id_mapping_msg_template.format(nt_seq_record.id, protein_acc_num,
                                                                            matched_protein_id))
                is_protein_seq_matched = True

        if is_protein_seq_matched:
            if is_ver_obsolete:
                ProcLog.log_obsolete_ver(msg = nt_seq_record.id)

            '''
            Group the protein product according to the ARG ontology class for subsequent outlier
            sequence detection
            '''
            if is_check_otl_annot:
                if latest_ver_protein_acc_num is not None:
                    genbank_protein_info = genbank_protein_info_set[latest_ver_protein_acc_num]
                else:
                    genbank_protein_info = genbank_protein_info_set[matched_protein_id]

                protein_seq_record = SeqRecord(Seq(genbank_protein_info.seq_str), id = nt_seq_record.id,
                                               name = '', description = '')

                Utils.group_protein_by_otl_class(otl_protein_seq_record_grps, protein_seq_record,
                                                otl_label_field_nums, config)
        else:
            ProcLog.log_seq_mismatch(nt_seq_record.id, is_ver_obsolete)

'''Validate ARG protein sequences annotated by NCBI protein accession numbers'''
for protein_acc_num, protein_seq_records in protein_id_protein_seq_records.items():
    protein_non_ver_acc_num = Utils.trim_version(protein_acc_num)

    if protein_non_ver_acc_num in matched_protein_non_ver_acc_nums:
        latest_ver_protein_acc_num = latest_ver_protein_acc_num_map[protein_non_ver_acc_num]
        is_ver_obsolete = (protein_acc_num != latest_ver_protein_acc_num and \
                           not Utils.is_non_version_acc_num(protein_acc_num))
    else:
        latest_ver_protein_acc_num = None
        is_ver_obsolete = False

    for protein_seq_record in protein_seq_records:
        protein_seq_str = str(protein_seq_record.seq)
        
        if latest_ver_protein_acc_num is not None:
            genbank_protein_info = genbank_protein_info_set[latest_ver_protein_acc_num]
            is_protein_seq_matched = (protein_seq_str == genbank_protein_info.seq_str)
        else:
            '''
            Search the ARG protein sequence in protein products identified by non-accession format
            identifiers
            '''
            matched_protein_id = Utils.match_non_acc_fmt_genbank_protein_seqs(protein_seq_str,
                                                                              genbank_protein_info_set,
                                                                              non_acc_fmt_protein_ids)
            if matched_protein_id is None:
                is_protein_seq_matched = False
            else:
                ProcLog.log_exec_msg(protein_id_mapping_msg_template.format(protein_seq_record.id, protein_acc_num,
                                                                            matched_protein_id))
                is_protein_seq_matched = True

        if is_protein_seq_matched:
            if is_ver_obsolete:
                ProcLog.log_obsolete_ver(msg = protein_seq_record.id)

            '''
            Group the ARG protein sequence according to the ARG ontology class for subsequent outlier
            sequence detection
            '''
            if is_check_otl_annot:
                Utils.group_protein_by_otl_class(otl_protein_seq_record_grps, protein_seq_record,
                                                otl_label_field_nums, config)
        else:
            ProcLog.log_seq_mismatch(protein_seq_record.id, is_ver_obsolete)

if is_check_otl_annot:
    cpu_count = len(os.sched_getaffinity(0))

    with Pool(cpu_count) as pool:
        otl_cls_outlier_seq_records = list(pool.imap_unordered(search_outlier_seqs,
                                                               otl_protein_seq_record_grps.items()))

    pool.join()

    for outlier_seq_records in otl_cls_outlier_seq_records:
        for seq_record in outlier_seq_records:
            ProcLog.log_false_otl_annot(msg = seq_record.id)

if args.exportlog:
    log_file_path = Utils.create_supp_file_path(args.seq_db_path, '.log')
    output_stream = open(log_file_path, 'w')
else:
    output_stream = sys.stdout

ProcLog.export_qc_check_logs(output_stream, is_check_otl_annot)

seq_record_count = seq_file_parser.get_seq_record_count()

'''Export refined sequences when necessary'''
if args.refine:
    refined_nt_seq_record_count = len(refined_nt_seq_records)
    if refined_nt_seq_record_count > 0:
        refined_seq_file_path = export_refined_seqs(refined_nt_seq_records, args.seq_db_path)
        refined_seq_stmt = ProcLog.create_summary_stmt(refined_nt_seq_record_count,
                                                       'exported to {}'.format(refined_seq_file_path), 'refined')
    else:
        refined_seq_stmt = ProcLog.create_summary_stmt(refined_nt_seq_record_count, 'refined')

    refined_seq_summary = [refined_seq_stmt]
    ProcLog.export_qc_check_summary(output_stream, seq_record_count, is_check_otl_annot, refined_seq_summary)
else:
    ProcLog.export_qc_check_summary(output_stream, seq_record_count, is_check_otl_annot)

if args.exportlog:
    output_stream.close()
