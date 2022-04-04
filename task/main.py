import base64
import logging
import os
import re
import boto3
import paramiko
import requests
import urllib3
from bs4 import BeautifulSoup
from cumulus_logger import CumulusLogger
from dateutil.parser import parse

from task.dgm import *

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging_level = logging.INFO if os.getenv('enable_logging', 'false').lower() == 'true' else logging.WARNING
rdg_logger = CumulusLogger(name='Recursive-Discover-Granules', level=logging_level)


class DiscoverGranules:
    """
    This class contains functions that fetch
    The metadata of the granules via a protocol X (HTTP/SFTP/S3)
    Compare the md5 of these granules with the ones in an S3
    It will return the files if they don't exist in S3 or the md5 doesn't match
    """

    def __init__(self, event):
        """
        Default values goes here
        """
        self.input = event.get('input')
        self.config = event.get('config')
        self.provider = self.config.get('provider')
        self.collection = self.config.get('collection')
        meta = self.collection.get('meta')
        self.discover_tf = meta.get('discover_tf')
        self.host = self.provider.get('host')

        aws_key_id = None
        aws_secret_key = None
        key_id_name = meta.get('aws_key_id_name')
        secret_key_name = meta.get('aws_secret_key_name')
        if key_id_name and secret_key_name:
            ssm_client = boto3.client('ssm')
            aws_key_id = ssm_client.get_parameter(Name=key_id_name).get('value')
            aws_secret_key = ssm_client.get_parameter(Name=secret_key_name).get('value')

        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_key_id,
            aws_secret_access_key=aws_secret_key
        )
        self.session = requests.Session()

        self.config_stack = self.config.get('stack')
        self.files_list = self.config.get('collection').get('files')

        db_suffix = meta.get('collection_type', 'static')
        db_filename = f'discover_granules_{db_suffix}.db'
        self.db_file_path = f'{os.getenv("efs_path", "/tmp")}/{db_filename}'

        self.lzards_backup = self.collection.get('files')

    @staticmethod
    def decode_decrypt(_ciphertext):
        kms_client = boto3.client('kms')
        decrypted_text = None
        try:
            response = kms_client.decrypt(
                CiphertextBlob=base64.b64decode(_ciphertext),
                KeyId=os.getenv('AWS_DECRYPT_KEY_ARN')
            )
            decrypted_text = response["Plaintext"].decode()
        except TypeError:
            rdg_logger.error('ciphertext was empty.')

        return decrypted_text

    def discover(self):
        """
        Helper function to kick off the entire discover process
        """
        output = {}
        granules = self.collection.get('meta', {}).get('granules', None)
        if self.input:
            # If there is input in the event then QueueGranules failed and we need to clean out the discovered granules
            names = []
            rdg_logger.warning(self.input.get('granules', {}))
            for granule in self.input.get('granules', {}):
                file = granule.get('files')[0]
                name = f'{file.get("path")}/{file.get("name")}'
                names.append(name)

            with initialize_db(self.db_file_path):
                num = Granule().delete_granules_by_names(names)

            rdg_logger.info(f'Cleaned {num} records from the database.')
            pass
        elif granules:
            # Re-ingest: Takes provided input and generates cumulus output.
            # TODO: This should be removed as it is wasteful to load the entire lambda just to generate output.
            rdg_logger.info(f'Received {len(granules)} to re-ingest.')
            granule_dict = {}
            for granule in granules:
                self.populate_dict(granule_dict, key=granule, etag=None, last_mod=None, size=None)
            output = self.cumulus_output_generator(granule_dict)
            pass
        else:
            # Discover granules
            granule_dict = self.discover_granules()
            if not granule_dict:
                rdg_logger.warning(f'Warning: Found 0 {self.collection.get("name")} granules at the provided location.')
            else:
                rdg_logger.info(f'Discovered {len(granule_dict)} {self.collection.get("name")} '
                                f'granules for update processing.')
            self.check_granule_updates_db(granule_dict)

            output = self.cumulus_output_generator(granule_dict)
            rdg_logger.info(f'Returning cumulus output for {len(output)} {self.collection.get("name")} granules.')

        rdg_logger.info(f'Discovered {len(output)} granules.')

        if os.getenv('no_return', 'false').lower() == 'true':
            rdg_logger.warning(f'no_return is set to true. No output will be returned.')
            output = []

        return {'granules': output}

    @staticmethod
    def populate_dict(target_dict, key, etag, last_mod, size):
        """
        Helper function to populate a dictionary with ETag and Last-Modified fields.
        Clarifying Note: This function works by exploiting the mutability of dictionaries
        :param target_dict: Dictionary to add a sub-dictionary to
        :param key: Value that will function as the new dictionary element key
        :param etag: The value of the ETag retrieved from the provider server
        :param last_mod: The value of the Last-Modified value retrieved from the provider server
        """
        target_dict[key] = {
            'ETag': etag,
            'Last-Modified': str(last_mod),
            'Size': size
        }

    @staticmethod
    def update_etag_lm(dict1, dict2, key):
        """
        Helper function to update the Etag and Last-Modified fields when comparing two dictionaries.
        Clarifying Note: This function works by exploiting the mutability of dictionaries
        :param dict1: The dictionary to be updated
        :param dict2: The source dictionary
        :param key: The key of the entry to be updated
        """
        dict1[key] = {
            'ETag': dict2.get(key).get('ETag'),
            'Last-Modified': dict2.get(key).get('Last-Modified'),
            'Size': dict2.get(key).get('Size'),
        }

    def fetch_session(self, url):
        """
        Establishes a session for requests.
        """
        return self.session.get(url, verify=False)

    def html_request(self, url_path: str):
        """
        :param url_path: The base URL where the files are served
        :return: The html of the page if the fetch is successful
        """
        opened_url = self.fetch_session(url_path)
        return BeautifulSoup(opened_url.text, features='html.parser')

    def headers_request(self, url_path: str):
        """
        Performs a head request for the given url.
        :param url_path The URL for the request
        :return Results of the request
        """
        return self.session.head(url_path).headers

    def get_headers(self, granule):
        """
        Gets the ETag and Last-Modified fields from a head response and returns it as a dictionary
        :param granule The url to request the header for
        :return temp a dictionary with {"key": {"ETag": "ETag", "Last-Modified": "Last-Modified"}}
        """
        head_resp = self.headers_request(granule)
        temp = {granule: {}}
        temp[granule]['ETag'] = str(head_resp.get('ETag', None))
        last_modified = head_resp.get('Last-Modified', None)
        if isinstance(last_modified, str):
            temp[granule]['Last-Modified'] = str(parse(last_modified))

        return temp

    def check_granule_updates_db(self, granule_dict: {}):
        """
        Checks stored granules and updates the datetime and ETag if updated. Expected values for duplicateHandling are
        error, replace, or skip
        :param granule_dict: Dictionary of granules to check
        :return Dictionary of granules that were new or updated
        """
        duplicates = str(self.collection.get('duplicateHandling', 'skip')).lower()
        force_replace = str(self.discover_tf.get('force_replace', 'false')).lower()
        # TODO: This is a temporary work around to resolve the issue with updated RSS granules not being re-ingested.
        if duplicates == 'replace' and force_replace == 'false':
            duplicates = 'skip'

        with initialize_db(self.db_file_path):
            getattr(Granule, f'db_{duplicates}')(Granule(), granule_dict)

        rdg_logger.info(f'{len(granule_dict)} granules remain after {duplicates} update processing.')

    def discover_granules(self):
        """
        Function to be called to trigger the granule discover process once the class has been initialized with the
        correct cumulus event
        """
        return getattr(self, f'prep_{self.provider["protocol"]}')()

    def prep_https(self):
        """
        Constructs an https url from the event provided at initialization and calls the http discovery function
        """
        return self.prep_http()

    def prep_http(self):
        """
        Constructs an http url from the event provided at initialization and calls the http discovery function
        """
        path = f'{self.provider["protocol"]}://{self.host.rstrip("/")}/' \
               f'{self.config["provider_path"].lstrip("/")}'
        return self.discover_granules_http(path, file_reg_ex=self.collection.get('granuleIdExtraction'),
                                           dir_reg_ex=self.discover_tf.get('dir_reg_ex'),
                                           depth=self.discover_tf.get('depth'))

    def discover_granules_http(self, url_path, file_reg_ex=None, dir_reg_ex=None, depth=0):
        """
        Fetch the link of the granules in the host url_path
        :param url_path: The base URL where the files are served
        :type url_path: string
        :param file_reg_ex: Regular expression used to filter files
        :type file_reg_ex: string
        :param dir_reg_ex: Regular expression used to filter directories
        :param depth: The positive number of levels to search down, will use the lesser of 3 or depth
        :return: links of files matching reg_ex (if reg_ex is defined)
        :rtype: dictionary of urls
        """
        granule_dict = {}
        depth = int(depth)
        fetched_html = self.html_request(url_path)
        directory_list = []
        for a_tag in fetched_html.findAll('a', href=True):
            url_segment = a_tag.get('href').rstrip('/').rsplit('/', 1)[-1]
            path = f'{url_path.rstrip("/")}/{url_segment}'
            head_resp = self.headers_request(path)
            etag = head_resp.get('ETag')
            last_modified = head_resp.get('Last-Modified')

            rdg_logger.info('##########')
            rdg_logger.info(f'Exploring a_tags for path: {path}')
            rdg_logger.info(f'ETag: {etag}')
            rdg_logger.info(f'Last-Modified: {last_modified}')

            if (etag is not None or last_modified is not None) and \
                    (file_reg_ex is None or re.search(file_reg_ex, url_segment)):
                rdg_logger.info(f'Discovered granule: {path}')

                granule_dict[path] = {}
                granule_dict[path]['ETag'] = str(etag)
                # The isinstance check is needed to prevent unit tests from trying to parse a MagicMock
                # object which will cause a crash during unit tests
                if isinstance(head_resp.get('Last-Modified'), str):
                    granule_dict[path]['Last-Modified'] = str(parse(last_modified).timestamp())
            elif (etag is None and last_modified is None) and \
                    (dir_reg_ex is None or re.search(dir_reg_ex, path)):
                directory_list.append(f'{path}/')
            else:
                rdg_logger.warning(f'Notice: {path} not processed as granule or directory. '
                                   f'The supplied regex may not match.')
        pass

        depth = min(abs(depth), 3)
        if depth > 0:
            for directory in directory_list:
                granule_dict.update(
                    self.discover_granules_http(url_path=directory, file_reg_ex=file_reg_ex,
                                                dir_reg_ex=dir_reg_ex, depth=(depth - 1))
                )

        return granule_dict

    @staticmethod
    def get_s3_resp_iterator(host, prefix, s3_client):
        """
        Returns an s3 paginator.
        :param host: The bucket.
        :param prefix: The path for the s3 granules.
        :param s3_client: S3 client to create paginator with.
        """
        s3_paginator = s3_client.get_paginator('list_objects')
        return s3_paginator.paginate(
            Bucket=host,
            Prefix=prefix,
            PaginationConfig={
                'PageSize': 1000
            }
        )

    def prep_s3(self):
        """
        Extracts the appropriate information for discovering granules using the S3 protocol
        """
        return self.discover_granules_s3(host=self.host, prefix=self.collection['meta']['provider_path'],
                                         file_reg_ex=self.collection.get('granuleIdExtraction'),
                                         dir_reg_ex=self.discover_tf.get('dir_reg_ex'))

    def discover_granules_s3(self, host: str, prefix: str, file_reg_ex=None, dir_reg_ex=None):
        """
        Fetch the link of the granules in the host s3 bucket.
        :param host: The bucket where the files are served.
        :param prefix: The path for the s3 granule.
        :param file_reg_ex: Regular expression used to filter files.
        :param dir_reg_ex: Regular expression used to filter directories.
        :return: links of files matching reg_ex (if reg_ex is defined).
        """
        rdg_logger.info(f'Discovering in s3://{host}/{prefix}.')
        response_iterator = self.get_s3_resp_iterator(host, prefix, self.s3_client)
        ret_dict = {}
        for page in response_iterator:
            for s3_object in page.get('Contents', {}):
                key = f'{self.provider.get("protocol")}://{self.provider.get("host")}/{s3_object["Key"]}'
                sections = str(key).rsplit('/', 1)
                key_dir = sections[0]
                file_name = sections[1]
                if (file_reg_ex is None or re.search(file_reg_ex, file_name)) and \
                        (dir_reg_ex is None or re.search(dir_reg_ex, key_dir)):
                    etag = s3_object['ETag'].strip('"')
                    last_modified = s3_object['LastModified'].timestamp()
                    size = s3_object['Size']

                    # rdg_logger.info(f'Found granule: {key}')
                    # rdg_logger.info(f'ETag: {etag}')
                    # rdg_logger.info(f'Last-Modified: {last_modified}')

                    self.populate_dict(ret_dict, key, etag, last_modified, size)

        return ret_dict

    def prep_sftp(self):
        """
        Handles extracting the necessary information from the event to discover granules using the SFTP protocol.
        """
        host = self.provider.get('host')
        port = self.provider.get('port')
        transport = paramiko.Transport((host, port))
        username_cypher = self.provider.get('username_cypher')
        password_cypher = self.provider.get('password_cypher')
        transport.connect(None, self.decode_decrypt(username_cypher), self.decode_decrypt(password_cypher))
        sftp_client = paramiko.SFTPClient.from_transport(transport)

        path = self.config.get('provider_path')
        file_reg_ex = self.collection.get('granuleIdExtraction', None)
        dir_reg_ex = self.discover_tf.get('dir_reg_ex', None)
        depth = self.discover_tf.get('depth')
        return self.discover_granules_sftp(sftp_client=sftp_client, path=path, file_reg_ex=file_reg_ex,
                                           dir_reg_ex=dir_reg_ex, depth=depth)

    def discover_granules_sftp(self, sftp_client: paramiko.SFTPClient, path: str, file_reg_ex: str = None,
                               dir_reg_ex: str = None,
                               depth: int = 0):
        """
        Discover granules on an SFTP provider
        :param sftp_client: An initialized paramiko SFTP client
        :param path: The location to begin discovery
        :param file_reg_ex: Regular expression used to filter files
        :param dir_reg_ex: Regular expression used to filter directories
        :param depth: The positive number of levels to search down, will use the lesser of 3 or depth
        :rtype: dictionary of discovered granules containing path, etag, and last modified dates
        """
        directory_list = []
        granule_dict = {}
        rdg_logger.info(f'Exploring path {path} depth {depth}')
        sftp_client.chdir(path)

        for dir_file in sftp_client.listdir():
            file_stat = sftp_client.stat(dir_file)
            file_type = str(file_stat)[0]
            if file_type == 'd' and (dir_reg_ex is None or re.search(dir_reg_ex, path)):
                rdg_logger.info(f'Found directory: {dir_file}')
                directory_list.append(dir_file)
            elif file_reg_ex is None or re.search(file_reg_ex, dir_file):
                populate_dict(granule_dict, f'{path}/{dir_file}', etag='N/A',
                              last_mod=file_stat.st_mtime, size=file_stat.st_size)
            else:
                rdg_logger.warning(f'Regex did not match dir_file: {dir_file}')

        depth = min(abs(depth), 3)
        if depth > 0:
            for directory in directory_list:
                granule_dict.update(
                    self.discover_granules_sftp(sftp_client, path=directory, file_reg_ex=file_reg_ex,
                                                dir_reg_ex=dir_reg_ex, depth=(depth - 1))
                )
        sftp_client.chdir('../')
        return granule_dict

    def get_path(self, key):
        """
        Extracts the path and file name from they key as needed for the cumulus output
        :param key: The full url where the file was discovered
        :return: A dictionary containing the path and name. <protocol>://<host>/some/path/and/file will return
        {'path': some/path/and, 'name': file}
        """
        temp = key.rsplit('/', 1)
        name = temp[1]
        replace_str = f'{self.provider.get("protocol")}://{self.provider.get("host")}/'
        path = temp[0].replace(replace_str, '')
        return {'path': path, 'name': name}

    def generate_cumulus_record(self, key, value, mapping):
        """
        Generates a single dictionary generator that yields the expected cumulus output for a granule
        :param key: The name of the file
        :param value: A dictionary of the form {'ETag': tag, 'Last-Modified': last_mod}
        :param mapping: Dictionary of each file extension and needed output fields from the event
        :return: A cumulus granule dictionary
        """
        epoch = value.get('Last-Modified')
        path_and_name_dict = self.get_path(key)
        version = self.collection.get('version', '')

        temp_dict = {}
        for reg_key, v in mapping.items():
            res = re.search(reg_key, path_and_name_dict.get('name'))
            if res:
                temp_dict.update(v)
                break

        checksum = ''
        checksum_type = ''
        if temp_dict.get('lzards'):
            checksum = value.get('ETag')
            checksum_type = 'md5'
            rdg_logger.info(f'LZARDS backing up: {key}')

        return {
            'granuleId': path_and_name_dict.get('name'),
            'dataType': self.collection.get('name', ''),
            'version': version,
            'files': [
                {
                    'bucket': f'{self.config_stack}-{temp_dict.get("bucket")}',
                    'checksum': checksum,
                    'checksumType': checksum_type,
                    'filename': key,
                    'name': path_and_name_dict.get('name'),
                    'path': path_and_name_dict.get('path'),
                    'size': value.get('Size'),
                    'time': epoch,
                    'type': '',
                }
            ]
        }

    def cumulus_output_generator(self, ret_dict):
        """
        Function to generate correctly formatted output for the next step in the workflow which is queue_granules.
        :param ret_dict: Dictionary containing only newly discovered granules.
        :return: Dictionary with a list of dictionaries formatted for the queue_granules workflow step.
        """
        # Extract the data from the files array in the event
        mapping = {}
        for file_dict in self.files_list:
            bucket = file_dict.get('bucket')
            reg = file_dict.get('regex')
            lzards = file_dict.get('lzards', {}).get('backup')
            mapping[reg] = {'bucket': bucket, 'lzards': lzards}

        return [self.generate_cumulus_record(k, v, mapping) for k, v in ret_dict.items()]


def populate_dict(target_dict, key, etag, last_mod, size):
    """
    Helper function to populate a dictionary with ETag and Last-Modified fields.
    Clarifying Note: This function works by exploiting the mutability of dictionaries
    :param target_dict: Dictionary to add a sub-dictionary to
    :param key: Value that will function as the new dictionary element key
    :param etag: The value of the ETag retrieved from the provider server
    :param last_mod: The value of the Last-Modified value retrieved from the provider server
    """
    target_dict[key] = {
        'ETag': etag,
        'Last-Modified': str(last_mod),
        'Size': size
    }

if __name__ == '__main__':
    pass
