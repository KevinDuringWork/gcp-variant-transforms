# Copyright 2020 Google LLC.
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

"""Tests for `sample_info_to_avro` module."""

import unittest

from apache_beam import transforms
from apache_beam.testing import test_pipeline
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to
import mock

from gcp_variant_transforms.beam_io import vcf_header_io
from gcp_variant_transforms.beam_io.vcf_parser import SampleNameEncoding
from gcp_variant_transforms.libs import sample_info_table_schema_generator
from gcp_variant_transforms.transforms import sample_info_to_avro

SAMPLE_LINE = (
    '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tSAMPLES\tSample1\tSample2')
TIME_MOCK = 1554426661.234567
EXPECTED_TIMESTAMP = 1554426660000000
def mocked_get_now():
  return TIME_MOCK


class ConvertSampleInfoToRowTest(unittest.TestCase):

  @mock.patch('gcp_variant_transforms.transforms.sample_info_to_avro.'
              'time.time',
              side_effect=mocked_get_now)
  def test_convert_sample_info_to_row(self, _):
    vcf_header_1 = vcf_header_io.VcfHeader(
        samples=SAMPLE_LINE, file_path='gs://bucket1/dir1/file1.vcf')
    vcf_header_2 = vcf_header_io.VcfHeader(
        samples=SAMPLE_LINE, file_path='gs://bucket1/dir1/file2.vcf')
    current_minute = EXPECTED_TIMESTAMP

    expected_rows = [
        {sample_info_table_schema_generator.SAMPLE_ID: 7715696391291253656,
         sample_info_table_schema_generator.SAMPLE_NAME: (
             'gs___bucket1_dir1_file1_vcf_Sample1'),
         sample_info_table_schema_generator.FILE_PATH: (
             'gs://bucket1/dir1/file1.vcf'),
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 5682150464643626236,
         sample_info_table_schema_generator.SAMPLE_NAME: (
             'gs___bucket1_dir1_file1_vcf_Sample2'),
         sample_info_table_schema_generator.FILE_PATH: (
             'gs://bucket1/dir1/file1.vcf'),
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 668336000922978678,
         sample_info_table_schema_generator.SAMPLE_NAME: (
             'gs___bucket1_dir1_file2_vcf_Sample1'),
         sample_info_table_schema_generator.FILE_PATH: (
             'gs://bucket1/dir1/file2.vcf'),
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 5498327443813165683,
         sample_info_table_schema_generator.SAMPLE_NAME: (
             'gs___bucket1_dir1_file2_vcf_Sample2'),
         sample_info_table_schema_generator.FILE_PATH: (
             'gs://bucket1/dir1/file2.vcf'),
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
    ]
    pipeline = test_pipeline.TestPipeline()
    bigquery_rows = (
        pipeline
        | transforms.Create([vcf_header_1, vcf_header_2])
        | 'ConvertToRow'
        >> transforms.ParDo(sample_info_to_avro.ConvertSampleInfoToRow(
            SampleNameEncoding.WITH_FILE_PATH), ))

    assert_that(bigquery_rows, equal_to(expected_rows))
    pipeline.run()

  @mock.patch('gcp_variant_transforms.transforms.sample_info_to_avro.'
              'time.time',
              side_effect=mocked_get_now)
  def test_convert_sample_info_to_row_without_file_in_hash(self, _):
    vcf_header_1 = vcf_header_io.VcfHeader(samples=SAMPLE_LINE,
                                           file_path='file_1')
    vcf_header_2 = vcf_header_io.VcfHeader(samples=SAMPLE_LINE,
                                           file_path='file_2')
    current_minute = EXPECTED_TIMESTAMP

    expected_rows = [
        {sample_info_table_schema_generator.SAMPLE_ID: 6365297890523177914,
         sample_info_table_schema_generator.SAMPLE_NAME: 'Sample1',
         sample_info_table_schema_generator.FILE_PATH: 'file_1',
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 8341768597576477893,
         sample_info_table_schema_generator.SAMPLE_NAME: 'Sample2',
         sample_info_table_schema_generator.FILE_PATH: 'file_1',
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 6365297890523177914,
         sample_info_table_schema_generator.SAMPLE_NAME: 'Sample1',
         sample_info_table_schema_generator.FILE_PATH: 'file_2',
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
        {sample_info_table_schema_generator.SAMPLE_ID: 8341768597576477893,
         sample_info_table_schema_generator.SAMPLE_NAME: 'Sample2',
         sample_info_table_schema_generator.FILE_PATH: 'file_2',
         sample_info_table_schema_generator.INGESTION_DATETIME: current_minute},
    ]
    pipeline = test_pipeline.TestPipeline()
    bigquery_rows = (
        pipeline
        | transforms.Create([vcf_header_1, vcf_header_2])
        | 'ConvertToRow'
        >> transforms.ParDo(sample_info_to_avro.ConvertSampleInfoToRow(
            SampleNameEncoding.WITHOUT_FILE_PATH), ))

    assert_that(bigquery_rows, equal_to(expected_rows))
    pipeline.run()
