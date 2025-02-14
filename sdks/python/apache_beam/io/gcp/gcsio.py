#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Google Cloud Storage client.

This library evolved from the Google App Engine GCS client available at
https://github.com/GoogleCloudPlatform/appengine-gcs-client.

**Updates to the I/O connector code**

For any significant updates to this I/O connector, please consider involving
corresponding code reviewers mentioned in
https://github.com/apache/beam/blob/master/sdks/python/OWNERS
"""

# pytype: skip-file

import errno
import io
import logging
import multiprocessing
import re
import threading
import time
import traceback
from itertools import islice
from typing import Optional
from typing import Union

import apache_beam
from apache_beam.internal.http_client import get_new_http
from apache_beam.internal.metrics.metric import ServiceCallMetric
from apache_beam.io.filesystemio import Downloader
from apache_beam.io.filesystemio import DownloaderStream
from apache_beam.io.filesystemio import PipeStream
from apache_beam.io.filesystemio import Uploader
from apache_beam.io.filesystemio import UploaderStream
from apache_beam.io.gcp import resource_identifiers
from apache_beam.metrics import monitoring_infos
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.utils import retry
from apache_beam.utils.annotations import deprecated

__all__ = ['GcsIO']

_LOGGER = logging.getLogger(__name__)

# Issue a friendlier error message if the storage library is not available.
# TODO(silviuc): Remove this guard when storage is available everywhere.
try:
  # pylint: disable=wrong-import-order, wrong-import-position
  # pylint: disable=ungrouped-imports
  from apitools.base.py.batch import BatchApiRequest
  from apitools.base.py.exceptions import HttpError
  from apitools.base.py import transfer
  from apache_beam.internal.gcp import auth
  from apache_beam.io.gcp.internal.clients import storage
except ImportError:
  raise ImportError(
      'Google Cloud Storage I/O not supported for this execution environment '
      '(could not import storage API client).')

# This is the size of each partial-file read operation from GCS.  This
# parameter was chosen to give good throughput while keeping memory usage at
# a reasonable level; the following table shows throughput reached when
# reading files of a given size with a chosen buffer size and informed the
# choice of the value, as of 11/2016:
#
# +---------------+------------+-------------+-------------+-------------+
# |               | 50 MB file | 100 MB file | 200 MB file | 400 MB file |
# +---------------+------------+-------------+-------------+-------------+
# | 8 MB buffer   | 17.12 MB/s | 22.67 MB/s  | 23.81 MB/s  | 26.05 MB/s  |
# | 16 MB buffer  | 24.21 MB/s | 42.70 MB/s  | 42.89 MB/s  | 46.92 MB/s  |
# | 32 MB buffer  | 28.53 MB/s | 48.08 MB/s  | 54.30 MB/s  | 54.65 MB/s  |
# | 400 MB buffer | 34.72 MB/s | 71.13 MB/s  | 79.13 MB/s  | 85.39 MB/s  |
# +---------------+------------+-------------+-------------+-------------+
DEFAULT_READ_BUFFER_SIZE = 16 * 1024 * 1024

# This is the number of seconds the library will wait for a partial-file read
# operation from GCS to complete before retrying.
DEFAULT_READ_SEGMENT_TIMEOUT_SECONDS = 60

# This is the size of chunks used when writing to GCS.
WRITE_CHUNK_SIZE = 8 * 1024 * 1024

# Maximum number of operations permitted in GcsIO.copy_batch() and
# GcsIO.delete_batch().
MAX_BATCH_OPERATION_SIZE = 100

# Batch endpoint URL for GCS.
# We have to specify an API specific endpoint here since Google APIs global
# batch endpoints will be deprecated on 03/25/2019.
# See https://developers.googleblog.com/2018/03/discontinuing-support-for-json-rpc-and.html.  # pylint: disable=line-too-long
# Currently apitools library uses a global batch endpoint by default:
# https://github.com/google/apitools/blob/master/apitools/base/py/batch.py#L152
# TODO: remove this constant and it's usage after apitools move to using an API
# specific batch endpoint or after Beam gcsio module start using a GCS client
# library that does not use global batch endpoints.
GCS_BATCH_ENDPOINT = 'https://www.googleapis.com/batch/storage/v1'


def parse_gcs_path(gcs_path, object_optional=False):
  """Return the bucket and object names of the given gs:// path."""
  match = re.match('^gs://([^/]+)/(.*)$', gcs_path)
  if match is None or (match.group(2) == '' and not object_optional):
    raise ValueError(
        'GCS path must be in the form gs://<bucket>/<object>. '
        f'Encountered {gcs_path!r}')
  return match.group(1), match.group(2)


def default_gcs_bucket_name(project, region):
  from hashlib import md5
  return 'dataflow-staging-%s-%s' % (
      region, md5(project.encode('utf8')).hexdigest())


def get_or_create_default_gcs_bucket(options):
  """Create a default GCS bucket for this project."""
  if getattr(options, 'dataflow_kms_key', None):
    _LOGGER.warning(
        'Cannot create a default bucket when --dataflow_kms_key is set.')
    return None

  project = getattr(options, 'project', None)
  region = getattr(options, 'region', None)
  if not project or not region:
    return None

  bucket_name = default_gcs_bucket_name(project, region)
  bucket = GcsIO(pipeline_options=options).get_bucket(bucket_name)
  if bucket:
    return bucket
  else:
    _LOGGER.warning(
        'Creating default GCS bucket for project %s: gs://%s',
        project,
        bucket_name)
    return GcsIO(pipeline_options=options).create_bucket(
        bucket_name, project, location=region)


class GcsIOError(IOError, retry.PermanentException):
  """GCS IO error that should not be retried."""
  pass


class GcsIO(object):
  """Google Cloud Storage I/O client."""
  def __init__(self, storage_client=None, pipeline_options=None):
    # type: (Optional[storage.StorageV1], Optional[Union[dict, PipelineOptions]]) -> None
    if storage_client is None:
      if not pipeline_options:
        pipeline_options = PipelineOptions()
      elif isinstance(pipeline_options, dict):
        pipeline_options = PipelineOptions.from_dictionary(pipeline_options)
      storage_client = storage.StorageV1(
          credentials=auth.get_service_credentials(pipeline_options),
          get_credentials=False,
          http=get_new_http(),
          response_encoding='utf8',
          additional_http_headers={
              "User-Agent": "apache-beam-%s" % apache_beam.__version__
          })
    self.client = storage_client
    self._rewrite_cb = None
    self.bucket_to_project_number = {}

  def get_project_number(self, bucket):
    if bucket not in self.bucket_to_project_number:
      bucket_metadata = self.get_bucket(bucket_name=bucket)
      if bucket_metadata:
        self.bucket_to_project_number[bucket] = bucket_metadata.projectNumber
      #  else failed to load the bucket metadata due to HttpError

    return self.bucket_to_project_number.get(bucket, None)

  def _set_rewrite_response_callback(self, callback):
    """For testing purposes only. No backward compatibility guarantees.

    Args:
      callback: A function that receives ``storage.RewriteResponse``.
    """
    self._rewrite_cb = callback

  def get_bucket(self, bucket_name):
    """Returns an object bucket from its name, or None if it does not exist."""
    try:
      request = storage.StorageBucketsGetRequest(bucket=bucket_name)
      return self.client.buckets.Get(request)
    except HttpError:
      return None

  def create_bucket(self, bucket_name, project, kms_key=None, location=None):
    """Create and return a GCS bucket in a specific project."""
    encryption = None
    if kms_key:
      encryption = storage.Bucket.EncryptionValue(kms_key)

    request = storage.StorageBucketsInsertRequest(
        bucket=storage.Bucket(
            name=bucket_name, location=location, encryption=encryption),
        project=project,
    )
    try:
      return self.client.buckets.Insert(request)
    except HttpError:
      return None

  def open(
      self,
      filename,
      mode='r',
      read_buffer_size=DEFAULT_READ_BUFFER_SIZE,
      mime_type='application/octet-stream'):
    """Open a GCS file path for reading or writing.

    Args:
      filename (str): GCS file path in the form ``gs://<bucket>/<object>``.
      mode (str): ``'r'`` for reading or ``'w'`` for writing.
      read_buffer_size (int): Buffer size to use during read operations.
      mime_type (str): Mime type to set for write operations.

    Returns:
      GCS file object.

    Raises:
      ValueError: Invalid open file mode.
    """
    if mode == 'r' or mode == 'rb':
      downloader = GcsDownloader(
          self.client,
          filename,
          buffer_size=read_buffer_size,
          get_project_number=self.get_project_number)
      return io.BufferedReader(
          DownloaderStream(
              downloader, read_buffer_size=read_buffer_size, mode=mode),
          buffer_size=read_buffer_size)
    elif mode == 'w' or mode == 'wb':
      uploader = GcsUploader(
          self.client,
          filename,
          mime_type,
          get_project_number=self.get_project_number)
      return io.BufferedWriter(
          UploaderStream(uploader, mode=mode), buffer_size=128 * 1024)
    else:
      raise ValueError('Invalid file open mode: %s.' % mode)

  @retry.with_exponential_backoff(
      retry_filter=retry.retry_on_server_errors_and_timeout_filter)
  def delete(self, path):
    """Deletes the object at the given GCS path.

    Args:
      path: GCS file path pattern in the form gs://<bucket>/<name>.
    """
    bucket, object_path = parse_gcs_path(path)
    request = storage.StorageObjectsDeleteRequest(
        bucket=bucket, object=object_path)
    try:
      self.client.objects.Delete(request)
    except HttpError as http_error:
      if http_error.status_code == 404:
        # Return success when the file doesn't exist anymore for idempotency.
        return
      raise

  # We intentionally do not decorate this method with a retry, as retrying is
  # handled in BatchApiRequest.Execute().
  def delete_batch(self, paths):
    """Deletes the objects at the given GCS paths.

    Args:
      paths: List of GCS file path patterns in the form gs://<bucket>/<name>,
             not to exceed MAX_BATCH_OPERATION_SIZE in length.

    Returns: List of tuples of (path, exception) in the same order as the paths
             argument, where exception is None if the operation succeeded or
             the relevant exception if the operation failed.
    """
    if not paths:
      return []

    paths = iter(paths)
    result_statuses = []
    while True:
      paths_chunk = list(islice(paths, MAX_BATCH_OPERATION_SIZE))
      if not paths_chunk:
        return result_statuses
      batch_request = BatchApiRequest(
          batch_url=GCS_BATCH_ENDPOINT,
          retryable_codes=retry.SERVER_ERROR_OR_TIMEOUT_CODES,
          response_encoding='utf-8')
      for path in paths_chunk:
        bucket, object_path = parse_gcs_path(path)
        request = storage.StorageObjectsDeleteRequest(
            bucket=bucket, object=object_path)
        batch_request.Add(self.client.objects, 'Delete', request)
      api_calls = batch_request.Execute(self.client._http)  # pylint: disable=protected-access
      for i, api_call in enumerate(api_calls):
        path = paths_chunk[i]
        exception = None
        if api_call.is_error:
          exception = api_call.exception
          # Return success when the file doesn't exist anymore for idempotency.
          if isinstance(exception, HttpError) and exception.status_code == 404:
            exception = None
        result_statuses.append((path, exception))
    return result_statuses

  @retry.with_exponential_backoff(
      retry_filter=retry.retry_on_server_errors_and_timeout_filter)
  def copy(
      self,
      src,
      dest,
      dest_kms_key_name=None,
      max_bytes_rewritten_per_call=None):
    """Copies the given GCS object from src to dest.

    Args:
      src: GCS file path pattern in the form gs://<bucket>/<name>.
      dest: GCS file path pattern in the form gs://<bucket>/<name>.
      dest_kms_key_name: Experimental. No backwards compatibility guarantees.
        Encrypt dest with this Cloud KMS key. If None, will use dest bucket
        encryption defaults.
      max_bytes_rewritten_per_call: Experimental. No backwards compatibility
        guarantees. Each rewrite API call will return after these many bytes.
        Used for testing.

    Raises:
      TimeoutError: on timeout.
    """
    src_bucket, src_path = parse_gcs_path(src)
    dest_bucket, dest_path = parse_gcs_path(dest)
    request = storage.StorageObjectsRewriteRequest(
        sourceBucket=src_bucket,
        sourceObject=src_path,
        destinationBucket=dest_bucket,
        destinationObject=dest_path,
        destinationKmsKeyName=dest_kms_key_name,
        maxBytesRewrittenPerCall=max_bytes_rewritten_per_call)
    response = self.client.objects.Rewrite(request)
    while not response.done:
      _LOGGER.debug(
          'Rewrite progress: %d of %d bytes, %s to %s',
          response.totalBytesRewritten,
          response.objectSize,
          src,
          dest)
      request.rewriteToken = response.rewriteToken
      response = self.client.objects.Rewrite(request)
      if self._rewrite_cb is not None:
        self._rewrite_cb(response)

    _LOGGER.debug('Rewrite done: %s to %s', src, dest)

  # We intentionally do not decorate this method with a retry, as retrying is
  # handled in BatchApiRequest.Execute().
  def copy_batch(
      self,
      src_dest_pairs,
      dest_kms_key_name=None,
      max_bytes_rewritten_per_call=None):
    """Copies the given GCS object from src to dest.

    Args:
      src_dest_pairs: list of (src, dest) tuples of gs://<bucket>/<name> files
                      paths to copy from src to dest, not to exceed
                      MAX_BATCH_OPERATION_SIZE in length.
      dest_kms_key_name: Experimental. No backwards compatibility guarantees.
        Encrypt dest with this Cloud KMS key. If None, will use dest bucket
        encryption defaults.
      max_bytes_rewritten_per_call: Experimental. No backwards compatibility
        guarantees. Each rewrite call will return after these many bytes. Used
        primarily for testing.

    Returns: List of tuples of (src, dest, exception) in the same order as the
             src_dest_pairs argument, where exception is None if the operation
             succeeded or the relevant exception if the operation failed.
    """
    if not src_dest_pairs:
      return []
    pair_to_request = {}
    for pair in src_dest_pairs:
      src_bucket, src_path = parse_gcs_path(pair[0])
      dest_bucket, dest_path = parse_gcs_path(pair[1])
      request = storage.StorageObjectsRewriteRequest(
          sourceBucket=src_bucket,
          sourceObject=src_path,
          destinationBucket=dest_bucket,
          destinationObject=dest_path,
          destinationKmsKeyName=dest_kms_key_name,
          maxBytesRewrittenPerCall=max_bytes_rewritten_per_call)
      pair_to_request[pair] = request
    pair_to_status = {}
    while True:
      pairs_in_batch = list(set(src_dest_pairs) - set(pair_to_status))
      if not pairs_in_batch:
        break
      batch_request = BatchApiRequest(
          batch_url=GCS_BATCH_ENDPOINT,
          retryable_codes=retry.SERVER_ERROR_OR_TIMEOUT_CODES,
          response_encoding='utf-8')
      for pair in pairs_in_batch:
        batch_request.Add(self.client.objects, 'Rewrite', pair_to_request[pair])
      api_calls = batch_request.Execute(self.client._http)  # pylint: disable=protected-access
      for pair, api_call in zip(pairs_in_batch, api_calls):
        src, dest = pair
        response = api_call.response
        if self._rewrite_cb is not None:
          self._rewrite_cb(response)
        if api_call.is_error:
          exception = api_call.exception
          # Translate 404 to the appropriate not found exception.
          if isinstance(exception, HttpError) and exception.status_code == 404:
            exception = (
                GcsIOError(errno.ENOENT, 'Source file not found: %s' % src))
          pair_to_status[pair] = exception
        elif not response.done:
          _LOGGER.debug(
              'Rewrite progress: %d of %d bytes, %s to %s',
              response.totalBytesRewritten,
              response.objectSize,
              src,
              dest)
          pair_to_request[pair].rewriteToken = response.rewriteToken
        else:
          _LOGGER.debug('Rewrite done: %s to %s', src, dest)
          pair_to_status[pair] = None

    return [(pair[0], pair[1], pair_to_status[pair]) for pair in src_dest_pairs]

  # We intentionally do not decorate this method with a retry, since the
  # underlying copy and delete operations are already idempotent operations
  # protected by retry decorators.
  def copytree(self, src, dest):
    """Renames the given GCS "directory" recursively from src to dest.

    Args:
      src: GCS file path pattern in the form gs://<bucket>/<name>/.
      dest: GCS file path pattern in the form gs://<bucket>/<name>/.
    """
    assert src.endswith('/')
    assert dest.endswith('/')
    for entry in self.list_prefix(src):
      rel_path = entry[len(src):]
      self.copy(entry, dest + rel_path)

  # We intentionally do not decorate this method with a retry, since the
  # underlying copy and delete operations are already idempotent operations
  # protected by retry decorators.
  def rename(self, src, dest):
    """Renames the given GCS object from src to dest.

    Args:
      src: GCS file path pattern in the form gs://<bucket>/<name>.
      dest: GCS file path pattern in the form gs://<bucket>/<name>.
    """
    self.copy(src, dest)
    self.delete(src)

  def exists(self, path):
    """Returns whether the given GCS object exists.

    Args:
      path: GCS file path pattern in the form gs://<bucket>/<name>.
    """
    try:
      self._gcs_object(path)  # gcs object
      return True
    except HttpError as http_error:
      if http_error.status_code == 404:
        # HTTP 404 indicates that the file did not exist
        return False
      else:
        # We re-raise all other exceptions
        raise

  def checksum(self, path):
    """Looks up the checksum of a GCS object.

    Args:
      path: GCS file path pattern in the form gs://<bucket>/<name>.
    """
    return self._gcs_object(path).crc32c

  def size(self, path):
    """Returns the size of a single GCS object.

    This method does not perform glob expansion. Hence the given path must be
    for a single GCS object.

    Returns: size of the GCS object in bytes.
    """
    return self._gcs_object(path).size

  def kms_key(self, path):
    """Returns the KMS key of a single GCS object.

    This method does not perform glob expansion. Hence the given path must be
    for a single GCS object.

    Returns: KMS key name of the GCS object as a string, or None if it doesn't
      have one.
    """
    return self._gcs_object(path).kmsKeyName

  def last_updated(self, path):
    """Returns the last updated epoch time of a single GCS object.

    This method does not perform glob expansion. Hence the given path must be
    for a single GCS object.

    Returns: last updated time of the GCS object in second.
    """
    return self._updated_to_seconds(self._gcs_object(path).updated)

  def _status(self, path):
    """For internal use only; no backwards-compatibility guarantees.

    Returns supported fields (checksum, kms_key, last_updated, size) of a
    single object as a dict at once.

    This method does not perform glob expansion. Hence the given path must be
    for a single GCS object.

    Returns: dict of fields of the GCS object.
    """
    gcs_object = self._gcs_object(path)
    file_status = {}
    if hasattr(gcs_object, 'crc32c'):
      file_status['checksum'] = gcs_object.crc32c
    if hasattr(gcs_object, 'kmsKeyName'):
      file_status['kms_key'] = gcs_object.kmsKeyName
    if hasattr(gcs_object, 'updated'):
      file_status['last_updated'] = self._updated_to_seconds(gcs_object.updated)
    if hasattr(gcs_object, 'size'):
      file_status['size'] = gcs_object.size
    return file_status

  @retry.with_exponential_backoff(
      retry_filter=retry.retry_on_server_errors_and_timeout_filter)
  def _gcs_object(self, path):
    """Returns a gcs object for the given path

    This method does not perform glob expansion. Hence the given path must be
    for a single GCS object.

    Returns: GCS object.
    """
    bucket, object_path = parse_gcs_path(path)
    request = storage.StorageObjectsGetRequest(
        bucket=bucket, object=object_path)
    return self.client.objects.Get(request)

  @deprecated(since='2.45.0', current='list_files')
  def list_prefix(self, path, with_metadata=False):
    """Lists files matching the prefix.

    ``list_prefix`` has been deprecated. Use `list_files` instead, which returns
    a generator of file information instead of a dict.

    Args:
      path: GCS file path pattern in the form gs://<bucket>/[name].
      with_metadata: Experimental. Specify whether returns file metadata.

    Returns:
      If ``with_metadata`` is False: dict of file name -> size; if
        ``with_metadata`` is True: dict of file name -> tuple(size, timestamp).
    """
    file_info = {}
    for file_metadata in self.list_files(path, with_metadata):
      file_info[file_metadata[0]] = file_metadata[1]

    return file_info

  def list_files(self, path, with_metadata=False):
    """Lists files matching the prefix.

    Args:
      path: GCS file path pattern in the form gs://<bucket>/[name].
      with_metadata: Experimental. Specify whether returns file metadata.

    Returns:
      If ``with_metadata`` is False: generator of tuple(file name, size); if
      ``with_metadata`` is True: generator of
      tuple(file name, tuple(size, timestamp)).
    """
    bucket, prefix = parse_gcs_path(path, object_optional=True)
    request = storage.StorageObjectsListRequest(bucket=bucket, prefix=prefix)
    file_info = set()
    counter = 0
    start_time = time.time()
    if with_metadata:
      _LOGGER.debug("Starting the file information of the input")
    else:
      _LOGGER.debug("Starting the size estimation of the input")
    while True:
      response = retry.with_exponential_backoff(
          retry_filter=retry.retry_on_server_errors_and_timeout_filter)(
              self.client.objects.List)(
                  request)

      for item in response.items:
        file_name = 'gs://%s/%s' % (item.bucket, item.name)
        if file_name not in file_info:
          file_info.add(file_name)
          counter += 1
          if counter % 10000 == 0:
            if with_metadata:
              _LOGGER.info(
                  "Finished computing file information of: %s files",
                  len(file_info))
            else:
              _LOGGER.info(
                  "Finished computing size of: %s files", len(file_info))

          if with_metadata:
            yield file_name, (item.size, self._updated_to_seconds(item.updated))
          else:
            yield file_name, item.size

      if response.nextPageToken:
        request.pageToken = response.nextPageToken
      else:
        break
    _LOGGER.log(
        # do not spam logs when list_prefix is likely used to check empty folder
        logging.INFO if counter > 0 else logging.DEBUG,
        "Finished listing %s files in %s seconds.",
        counter,
        time.time() - start_time)

  @staticmethod
  def _updated_to_seconds(updated):
    """Helper function transform the updated field of response to seconds"""
    return (
        time.mktime(updated.timetuple()) - time.timezone +
        updated.microsecond / 1000000.0)


class GcsDownloader(Downloader):
  def __init__(self, client, path, buffer_size, get_project_number):
    self._client = client
    self._path = path
    self._bucket, self._name = parse_gcs_path(path)
    self._buffer_size = buffer_size
    self._get_project_number = get_project_number

    # Create a request count metric
    resource = resource_identifiers.GoogleCloudStorageBucket(self._bucket)
    labels = {
        monitoring_infos.SERVICE_LABEL: 'Storage',
        monitoring_infos.METHOD_LABEL: 'Objects.get',
        monitoring_infos.RESOURCE_LABEL: resource,
        monitoring_infos.GCS_BUCKET_LABEL: self._bucket
    }
    project_number = self._get_project_number(self._bucket)
    if project_number:
      labels[monitoring_infos.GCS_PROJECT_ID_LABEL] = str(project_number)
    else:
      _LOGGER.debug(
          'Possibly missing storage.buckets.get permission to '
          'bucket %s. Label %s is not added to the counter because it '
          'cannot be identified.',
          self._bucket,
          monitoring_infos.GCS_PROJECT_ID_LABEL)

    service_call_metric = ServiceCallMetric(
        request_count_urn=monitoring_infos.API_REQUEST_COUNT_URN,
        base_labels=labels)

    # Get object state.
    self._get_request = (
        storage.StorageObjectsGetRequest(
            bucket=self._bucket, object=self._name))
    try:
      metadata = self._get_object_metadata(self._get_request)
    except HttpError as http_error:
      service_call_metric.call(http_error)
      if http_error.status_code == 404:
        raise IOError(errno.ENOENT, 'Not found: %s' % self._path)
      else:
        _LOGGER.error(
            'HTTP error while requesting file %s: %s', self._path, http_error)
        raise
    else:
      service_call_metric.call('ok')

    self._size = metadata.size

    # Ensure read is from file of the correct generation.
    self._get_request.generation = metadata.generation

    # Initialize read buffer state.
    self._download_stream = io.BytesIO()
    self._downloader = transfer.Download(
        self._download_stream,
        auto_transfer=False,
        chunksize=self._buffer_size,
        num_retries=20)

    try:
      self._client.objects.Get(self._get_request, download=self._downloader)
      service_call_metric.call('ok')
    except HttpError as e:
      service_call_metric.call(e)
      raise

  @retry.with_exponential_backoff(
      retry_filter=retry.retry_on_server_errors_and_timeout_filter)
  def _get_object_metadata(self, get_request):
    return self._client.objects.Get(get_request)

  @property
  def size(self):
    return self._size

  def get_range(self, start, end):
    self._download_stream.seek(0)
    self._download_stream.truncate(0)
    self._downloader.GetRange(start, end - 1)
    return self._download_stream.getvalue()


class GcsUploader(Uploader):
  def __init__(self, client, path, mime_type, get_project_number):
    self._client = client
    self._path = path
    self._bucket, self._name = parse_gcs_path(path)
    self._mime_type = mime_type
    self._get_project_number = get_project_number

    # Set up communication with child thread.
    parent_conn, child_conn = multiprocessing.Pipe()
    self._child_conn = child_conn
    self._conn = parent_conn

    # Set up uploader.
    self._insert_request = (
        storage.StorageObjectsInsertRequest(
            bucket=self._bucket, name=self._name))
    self._upload = transfer.Upload(
        PipeStream(self._child_conn),
        self._mime_type,
        chunksize=WRITE_CHUNK_SIZE)
    self._upload.strategy = transfer.RESUMABLE_UPLOAD

    # Start uploading thread.
    self._upload_thread = threading.Thread(target=self._start_upload)
    self._upload_thread.daemon = True
    self._upload_thread.last_error = None
    self._upload_thread.start()

  # TODO(silviuc): Refactor so that retry logic can be applied.
  # There is retry logic in the underlying transfer library but we should make
  # it more explicit so we can control the retry parameters.
  @retry.no_retries  # Using no_retries marks this as an integration point.
  def _start_upload(self):
    # This starts the uploader thread.  We are forced to run the uploader in
    # another thread because the apitools uploader insists on taking a stream
    # as input. Happily, this also means we get asynchronous I/O to GCS.
    #
    # The uploader by default transfers data in chunks of 1024 * 1024 bytes at
    # a time, buffering writes until that size is reached.

    project_number = self._get_project_number(self._bucket)

    # Create a request count metric
    resource = resource_identifiers.GoogleCloudStorageBucket(self._bucket)
    labels = {
        monitoring_infos.SERVICE_LABEL: 'Storage',
        monitoring_infos.METHOD_LABEL: 'Objects.insert',
        monitoring_infos.RESOURCE_LABEL: resource,
        monitoring_infos.GCS_BUCKET_LABEL: self._bucket,
        monitoring_infos.GCS_PROJECT_ID_LABEL: str(project_number)
    }
    service_call_metric = ServiceCallMetric(
        request_count_urn=monitoring_infos.API_REQUEST_COUNT_URN,
        base_labels=labels)
    try:
      self._client.objects.Insert(self._insert_request, upload=self._upload)
      service_call_metric.call('ok')
    except Exception as e:  # pylint: disable=broad-except
      service_call_metric.call(e)
      _LOGGER.error(
          'Error in _start_upload while inserting file %s: %s',
          self._path,
          traceback.format_exc())
      self._upload_thread.last_error = e
    finally:
      self._child_conn.close()

  def put(self, data):
    try:
      self._conn.send_bytes(data.tobytes())
    except EOFError:
      if self._upload_thread.last_error is not None:
        raise self._upload_thread.last_error  # pylint: disable=raising-bad-type
      raise

  def finish(self):
    self._conn.close()
    # TODO(udim): Add timeout=DEFAULT_HTTP_TIMEOUT_SECONDS * 2 and raise if
    # isAlive is True.
    self._upload_thread.join()
    # Check for exception since the last put() call.
    if self._upload_thread.last_error is not None:
      raise self._upload_thread.last_error  # pylint: disable=raising-bad-type
