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

"""Implements a variant merge stategy that moves fields to calls."""


import hashlib
import re
from typing import Iterable, Set  # pylint: disable=unused-import

from apache_beam.io.gcp.internal.clients import bigquery  # pylint: disable=unused-import

from gcp_variant_transforms.beam_io.vcfio import Variant
from gcp_variant_transforms.libs import bigquery_util
from gcp_variant_transforms.libs.variant_merge import variant_merge_strategy

__all__ = ['MoveToCallsStrategy']


class MoveToCallsStrategy(variant_merge_strategy.VariantMergeStrategy):
  """A merging strategy that moves fields to the corresponding calls records.

  Variants will be merged across files using
  'reference_name:start:end:reference_bases:alternate_bases' as key. INFO
  fields would be moved to calls if they match
  `info_keys_to_move_to_calls_regex`. Otherwise, one will be chosen as
  representatve (in no particular order) among the merged variants.
  Filters will be merged across all variants matching the key and the highest
  quality score will be chosen as representative for the merged variants.
  The filters and quality fields can be optionally copied to their associated
  calls using `copy_quality_to_calls` and `copy_filter_to_calls` options.

  Note: if a field is set to be moved from INFO to calls, then it must not
  already exist in calls (i.e. specified by FORMAT in the VCF header).
  """

  def __init__(self, info_keys_to_move_to_calls_regex, copy_quality_to_calls,
               copy_filter_to_calls):
    # type: (str, bool, bool) -> None
    """Initializes the strategy.

    Args:
      info_keys_to_move_to_calls_regex: A regular expression specifying info
        fields that should be moved to calls.
      copy_quality_to_calls: Whether to copy the quality field to the associated
        calls in each record.
      copy_filter_to_calls: Whether to copy filter field to the associated calls
        in each record.
    """
    self._info_keys_to_move_to_calls_re = (
        re.compile(info_keys_to_move_to_calls_regex)
        if info_keys_to_move_to_calls_regex else None)
    self._copy_quality_to_calls = copy_quality_to_calls
    self._copy_filter_to_calls = copy_filter_to_calls

  def move_data_to_calls(self, variant):
    # type: (Variant) -> None
    """Moves filters, calls, and info items to the variant's calls based on the
    strategy's initialization parameters.

    Args:
      variant: The variant whose filters, quality, and info items will be moved
        to its calls if specified.
    """
    additional_call_info = {}
    if self._should_copy_filter_to_calls():
      additional_call_info[
          bigquery_util.ColumnKeyConstants.FILTER] = variant.filters
    if self._should_copy_quality_to_calls():
      additional_call_info[
          bigquery_util.ColumnKeyConstants.QUALITY] = variant.quality
    for info_key, info_value in variant.info.items():
      if self._should_move_info_key_to_calls(info_key):
        additional_call_info[info_key] = info_value
    for call in variant.calls:
      call.info.update(additional_call_info)

  def move_data_to_merged(self, variant, merged_variant):
    # type: (Variant, Variant) -> None
    """Moves items from the variant's info to merged_variant.

    Args:
      variant: The variant whose info items will be moved to `merged_variant` if
        specified.
      merged_variant: The variant who will receive the info items of `variant`
        if specified.
    """
    for info_key, info_value in variant.info.items():
      if not self._should_move_info_key_to_calls(info_key):
        merged_variant.info[info_key] = info_value

  def get_merged_variants(self, variants, unused_key=None):
    # type: (List[Variant], str) -> List[Variant]
    if not variants:
      return []
    merged_variant = None
    for variant in variants:
      if not merged_variant:
        merged_variant = Variant(reference_name=variant.reference_name,
                                 start=variant.start,
                                 end=variant.end,
                                 reference_bases=variant.reference_bases,
                                 alternate_bases=variant.alternate_bases)
      # Since we use hash function in generating the merge key, there is
      # a chance (extremely low though) to have variants with different
      # `reference_bases` or `alternate_base` here due to a collision in
      # the hash function.
      assert variant.reference_bases == merged_variant.reference_bases, (
          'Cannot merge variants with different reference bases. {} vs {}'
          .format(variant.reference_bases, merged_variant.reference_bases))
      assert variant.alternate_bases == merged_variant.alternate_bases, (
          'Cannot merge variants with different alternate bases. {} vs {}'
          .format(variant.alternate_bases, merged_variant.alternate_bases))

      merged_variant.names.extend(variant.names)
      merged_variant.filters.extend(variant.filters)
      if (merged_variant.quality is not None and
          variant.quality is not None):
        merged_variant.quality = max(merged_variant.quality, variant.quality)
      elif merged_variant.quality is None:
        merged_variant.quality = variant.quality

      self.move_data_to_calls(variant)
      self.move_data_to_merged(variant, merged_variant)

      merged_variant.calls.extend(variant.calls)

    # Deduplicate names and filters.
    merged_variant.names = sorted(set(merged_variant.names))
    merged_variant.filters = sorted(set(merged_variant.filters))
    return [merged_variant]

  def get_merge_keys(self, variant):
    yield ':'.join(
        [str(x) for x in [
            variant.reference_name or '',
            variant.start or '',
            variant.end or '',
            self._get_hash(variant.reference_bases or ''),
            self._get_hash(','.join(variant.alternate_bases or []))]])

  def modify_bigquery_schema(self, schema, info_keys):
    # type: (bigquery.TableSchema, Set[str]) -> None
    # Find the calls record so that it's easier to reference it below.
    calls_record = None
    for field in schema.fields:
      if field.name == bigquery_util.ColumnKeyConstants.CALLS:
        calls_record = field
        break
    if not calls_record:
      raise ValueError('calls record must exist in the schema.')

    existing_calls_keys = {field.name for field in calls_record.fields}
    updated_fields = []
    for field in schema.fields:
      if (self._should_copy_filter_to_calls() and
          field.name == bigquery_util.ColumnKeyConstants.FILTER):
        if bigquery_util.ColumnKeyConstants.FILTER in existing_calls_keys:
          self._raise_duplicate_key_error(
              bigquery_util.ColumnKeyConstants.FILTER,
              'should_copy_filter_to_calls')
        calls_record.fields.append(field)
        updated_fields.append(field)
      elif (self._should_copy_quality_to_calls() and
            field.name == bigquery_util.ColumnKeyConstants.QUALITY):
        if bigquery_util.ColumnKeyConstants.QUALITY in existing_calls_keys:
          self._raise_duplicate_key_error(
              bigquery_util.ColumnKeyConstants.QUALITY,
              'should_copy_quality_to_calls')
        calls_record.fields.append(field)
        updated_fields.append(field)
      elif (field.name in info_keys and
            self._should_move_info_key_to_calls(field.name)):
        if field.name in existing_calls_keys:
          self._raise_duplicate_key_error(field.name,
                                          'info_keys_to_move_to_calls_regex')
        calls_record.fields.append(field)
      else:
        updated_fields.append(field)
    schema.fields = updated_fields

  def _get_hash(self, value):
    return hashlib.md5(value.encode('utf-8')).hexdigest()

  def _should_move_info_key_to_calls(self, info_key):
    return bool(self._info_keys_to_move_to_calls_re and
                self._info_keys_to_move_to_calls_re.match(info_key))

  def _should_copy_filter_to_calls(self):
    return self._copy_filter_to_calls

  def _should_copy_quality_to_calls(self):
    return self._copy_quality_to_calls

  def _raise_duplicate_key_error(self, key, flag_name):
    raise ValueError(
        'The field "%s" already exists in calls, but %s flag also moves a '
        'field with the same name to calls. Please either change the flag '
        'or rename the field.' % (key, flag_name))
