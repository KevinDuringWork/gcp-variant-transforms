# Copyright 2017 Google Inc.  All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Pipeline for loading VCF files to BigQuery.

Run locally:
python -m gcp_variant_transforms.vcf_to_bq \
  --input_pattern <path to VCF file(s)> \
  --output_table projectname:bigquerydataset.tablename

Run on Dataflow:
python -m gcp_variant_transforms.vcf_to_bq \
  --input_pattern gs://bucket/vcfs/vcffile.vcf \
  --output_table projectname:bigquerydataset.tablename \
  --project projectname \
  --staging_location gs://bucket/staging \
  --temp_location gs://bucket/temp \
  --job_name vcf-to-bq \
  --setup_file ./setup.py \
  --runner DataflowRunner
"""

from __future__ import absolute_import

import argparse
import logging
import re

import apache_beam as beam
from apache_beam.io.gcp.internal.clients import bigquery
from apache_beam.options.pipeline_options import PipelineOptions
from apitools.base.py import exceptions
from oauth2client.client import GoogleCredentials

# TODO: Replace with the version from Beam SDK once that is released.
from gcp_variant_transforms.beam_io import vcfio
from gcp_variant_transforms.libs import vcf_header_parser
from gcp_variant_transforms.libs.variant_merge import move_to_calls_strategy
from gcp_variant_transforms.transforms import filter_variants
from gcp_variant_transforms.transforms import merge_variants
from gcp_variant_transforms.transforms import variant_to_bigquery

# List of supported merge strategies for variants.
# - NONE: Variants will not be merged across files.
# - MOVE_TO_CALLS: uses libs.variant_merge.move_to_calls_strategy
#   for merging. Please see the documentation in that file for details.
_VARIANT_MERGE_STRATEGIES = ['NONE', 'MOVE_TO_CALLS']


def _get_variant_merge_strategy(known_args):
  if (not known_args.variant_merge_strategy or
      known_args.variant_merge_strategy == 'NONE'):
    return None
  elif known_args.variant_merge_strategy == 'MOVE_TO_CALLS':
    return move_to_calls_strategy.MoveToCallsStrategy(
        known_args.info_keys_to_move_to_calls_regex,
        known_args.copy_quality_to_calls,
        known_args.copy_filter_to_calls)
  else:
    raise ValueError('Merge strategy is not supported.')


def _validate_bq_path(output_table, client=None):
  output_table_re_match = re.match(
      r'^((?P<project>.+):)(?P<dataset>\w+)\.(?P<table>[\w\$]+)$',
      output_table)
  if not output_table_re_match:
    raise ValueError(
        'Expected a table reference (PROJECT:DATASET.TABLE) instead of %s.' % (
            output_table))
  try:
    if not client:
      credentials = GoogleCredentials.get_application_default().create_scoped(
          ['https://www.googleapis.com/auth/bigquery'])
      client = bigquery.BigqueryV2(credentials=credentials)
    client.datasets.Get(bigquery.BigqueryDatasetsGetRequest(
        projectId=output_table_re_match.group('project'),
        datasetId=output_table_re_match.group('dataset')))
  except exceptions.HttpError as e:
    if e.status_code == 404:
      raise ValueError('Dataset %s:%s does not exist.' %
                       (output_table_re_match.group('project'),
                        output_table_re_match.group('dataset')))
    else:
      # For the rest of the errors, use BigQuery error message.
      raise


def _validate_args(known_args):
  _validate_bq_path(known_args.output_table)

  if known_args.variant_merge_strategy != 'MOVE_TO_CALLS':
    if known_args.info_keys_to_move_to_calls_regex:
      raise ValueError(
          '--info_keys_to_move_to_calls_regex requires '
          '--variant_merge_strategy MOVE_TO_CALLS.')
    if known_args.copy_quality_to_calls:
      raise ValueError(
          '--copy_quality_to_calls requires '
          '--variant_merge_strategy MOVE_TO_CALLS.')
    if known_args.copy_filter_to_calls:
      raise ValueError(
          '--copy_filter_to_calls requires '
          '--variant_merge_strategy MOVE_TO_CALLS.')


def run(argv=None):
  """Runs VCF to BigQuery pipeline."""

  parser = argparse.ArgumentParser()
  parser.register('type', 'bool', lambda v: v.lower() == 'true')

  # I/O options.
  parser.add_argument('--input_pattern',
                      dest='input_pattern',
                      required=True,
                      help='Input pattern for VCF files to process.')
  parser.add_argument('--output_table',
                      dest='output_table',
                      required=True,
                      help='BigQuery table to store the results.')
  parser.add_argument(
      '--representative_header_file',
      dest='representative_header_file',
      default='',
      help=('If provided, header values from the provided file will be used as '
            'representative for all files matching input_pattern. '
            'In particular, this will be used to generate the BigQuery schema. '
            'If not provided, header values from all files matching '
            'input_pattern will be merged by key. Only one value will be '
            'chosen (in no particular order) in cases where multiple files use '
            'the same key. Providing this file improves performance if a '
            'large number of files are specified by input_pattern. '
            'Note that each VCF file must still contain valid header files '
            'even if this is provided.'))

  # Output schema options.
  parser.add_argument(
      '--split_alternate_allele_info_fields',
      dest='split_alternate_allele_info_fields',
      type='bool', default=True, nargs='?', const=True,
      help=('If true, all INFO fields with Number=A (i.e. one value for each '
            'alternate allele) will be stored under the alternate_bases '
            'record. If false, they will be stored with the rest of the INFO '
            'fields. Setting this option to true makes querying the data '
            'easier, because it avoids having to map each field with the '
            'corresponding alternate record while querying.'))

  # Merging logic.
  parser.add_argument(
      '--variant_merge_strategy',
      dest='variant_merge_strategy',
      default='NONE',
      choices=_VARIANT_MERGE_STRATEGIES,
      help=('Variant merge strategy to use. Set to NONE if variants should '
            'not be merged across files.'))
  # Configs for MOVE_TO_CALLS strategy.
  parser.add_argument(
      '--info_keys_to_move_to_calls_regex',
      dest='info_keys_to_move_to_calls_regex',
      default='',
      help=('Regular expression specifying the INFO keys to move to the '
            'associated calls in each VCF file. '
            'Requires variant_merge_strategy=MOVE_TO_CALLS.'))
  parser.add_argument(
      '--copy_quality_to_calls',
      dest='copy_quality_to_calls',
      type='bool', default=False, nargs='?', const=True,
      help=('If true, the QUAL field for each record will be copied to '
            'the associated calls in each VCF file. '
            'Requires variant_merge_strategy=MOVE_TO_CALLS.'))
  parser.add_argument(
      '--copy_filter_to_calls',
      dest='copy_filter_to_calls',
      type='bool', default=False, nargs='?', const=True,
      help=('If true, the FILTER field for each record will be copied to '
            'the associated calls in each VCF file. '
            'Requires variant_merge_strategy=MOVE_TO_CALLS.'))

  parser.add_argument(
      '--reference_names',
      dest='reference_names', default=None, nargs='+',
      help=('A list of reference names (separated by a space) to load '
            'to BigQuery. If this parameter is not specified, all '
            'references will be kept.'))

  known_args, pipeline_args = parser.parse_known_args(argv)
  _validate_args(known_args)

  variant_merger = _get_variant_merge_strategy(known_args)
  # Retrieve merged headers prior to launching the pipeline. This is needed
  # since the BigQuery schema cannot yet be dynamically created based on input.
  # See https://issues.apache.org/jira/browse/BEAM-2801.
  header_fields = vcf_header_parser.get_merged_vcf_headers(
      known_args.representative_header_file or known_args.input_pattern)

  pipeline_options = PipelineOptions(pipeline_args)
  with beam.Pipeline(options=pipeline_options) as p:
    variants = (p
                | 'ReadFromVcf' >> vcfio.ReadFromVcf(known_args.input_pattern)
                | 'FilterVariants' >> filter_variants.FilterVariants(
                    reference_names=known_args.reference_names))
    if variant_merger:
      variants |= (
          'MergeVariants' >> merge_variants.MergeVariants(variant_merger))
    _ = (variants |
         'VariantToBigQuery' >> variant_to_bigquery.VariantToBigQuery(
             known_args.output_table,
             header_fields,
             variant_merger,
             known_args.split_alternate_allele_info_fields))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()
