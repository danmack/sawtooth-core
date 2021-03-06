# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import logging
import time
import urllib
import urllib2
import urlparse
try:  # weird windows behavior
    from enum import IntEnum as Enum
except ImportError:
    from enum import Enum

from gossip import node
from gossip import signed_object
from gossip.common import json2dict
from gossip.common import cbor2dict
from gossip.common import dict2cbor
from gossip.common import pretty_print_dict
from journal import global_store_manager
from journal import transaction
from sawtooth.exceptions import ClientException
from sawtooth.exceptions import InvalidTransactionError
from sawtooth.exceptions import MessageException


LOGGER = logging.getLogger(__name__)


# Map HTTP status codes to their corresponding transaction status
class TransactionStatus(Enum):
    committed = 200
    pending = 302
    not_found = 404
    internal_server_error = 500,
    server_busy = 503


class _Communication(object):
    """
    A class to encapsulate communication with the validator
    """

    def __init__(self, base_url):
        self._base_url = base_url.rstrip('/')
        self._proxy_handler = urllib2.ProxyHandler({})
        self._cookie = None

    @property
    def base_url(self):
        return self._base_url

    def headrequest(self, path):
        """
        Send an HTTP head request to the validator. Return the result code.
        """

        url = urlparse.urljoin(self._base_url, path)

        LOGGER.debug('get content from url <%s>', url)

        try:
            request = urllib2.Request(url)
            request.get_method = lambda: 'HEAD'
            opener = urllib2.build_opener(self._proxy_handler)
            response = opener.open(request, timeout=30)

        except urllib2.HTTPError as err:
            # in this case it isn't really an error since we are just looking
            # for the status code
            return err.code

        except urllib2.URLError as err:
            LOGGER.warn('operation failed: %s', err.reason)
            raise MessageException('operation failed: {0}'.format(err.reason))

        except:
            LOGGER.warn('no response from server')
            raise MessageException('no response from server')

        return response.code

    def _print_error_information_from_server(self, err):
        if err.code == 400:
            err_content = err.read()
            LOGGER.warn('Error from server, detail information: %s',
                        err_content)

    def getmsg(self, path, timeout=10):
        """
        Send an HTTP get request to the validator. If the resulting content
        is in JSON form, parse it & return the corresponding dictionary.
        """

        url = urlparse.urljoin(self._base_url, path)

        LOGGER.debug('get content from url <%s>', url)

        try:
            request = urllib2.Request(url)
            opener = urllib2.build_opener(self._proxy_handler)
            response = opener.open(request, timeout=timeout)

        except urllib2.HTTPError as err:
            LOGGER.warn('operation failed with response: %s', err.code)
            self._print_error_information_from_server(err)
            raise MessageException(
                'operation failed with response: {0}'.format(err.code))

        except urllib2.URLError as err:
            LOGGER.warn('operation failed: %s', err.reason)
            raise MessageException('operation failed: {0}'.format(err.reason))

        except:
            LOGGER.warn('no response from server')
            raise MessageException('no response from server')

        content = response.read()
        headers = response.info()
        response.close()

        encoding = headers.get('Content-Type')

        if encoding == 'application/json':
            return json2dict(content)
        elif encoding == 'application/cbor':
            return cbor2dict(content)
        else:
            return content

    def postmsg(self, msgtype, info):
        """
        Post a transaction message to the validator, parse the returning CBOR
        and return the corresponding dictionary.
        """

        data = dict2cbor(info)
        datalen = len(data)
        url = urlparse.urljoin(self._base_url, msgtype)

        LOGGER.debug('post transaction to %s with DATALEN=%d, DATA=<%s>', url,
                     datalen, data)

        try:
            request = urllib2.Request(url, data,
                                      {'Content-Type': 'application/cbor',
                                       'Content-Length': datalen})

            if self._cookie:
                request.add_header('cookie', self._cookie)

            opener = urllib2.build_opener(self._proxy_handler)
            response = opener.open(request, timeout=10)
            if not self._cookie:
                self._cookie = response.headers.get('Set-Cookie')
        except urllib2.HTTPError as err:
            content = err.read()
            if content is not None:
                headers = err.info()
                encoding = headers.get('Content-Type')

                if encoding == 'application/json':
                    value = json2dict(content)
                elif encoding == 'application/cbor':
                    value = cbor2dict(content)
                else:
                    LOGGER.warn('operation failed with response: %s', err.code)
                    raise MessageException(
                        'operation failed with response: {0}'.format(err.code))
                LOGGER.warn('operation failed with response: %s %s',
                            err.code, str(value))
                if "errorType" in value:
                    if value['errorType'] == "InvalidTransactionError":
                        raise InvalidTransactionError(
                            value['error'] if 'error' in value else value)
                    else:
                        raise MessageException(str(value))
            else:
                raise MessageException(
                    'operation failed with response: {0}'.format(err.code))
        except urllib2.URLError as err:
            LOGGER.warn('operation failed: %s', err.reason)
            raise MessageException('operation failed: {0}'.format(err.reason))

        except:
            LOGGER.warn('no response from server')
            raise MessageException('no response from server')

        content = response.read()
        headers = response.info()
        response.close()

        encoding = headers.get('Content-Type')

        if encoding == 'application/json':
            value = json2dict(content)
        elif encoding == 'application/cbor':
            value = cbor2dict(content)
        else:
            LOGGER.info('server responds with message %s of type %s', content,
                        encoding)
            return None

        LOGGER.debug(pretty_print_dict(value))
        return value


class _ClientState(object):
    def __init__(self,
                 client,
                 state_type=global_store_manager.KeyValueStore):
        self._client = client
        self._state_type = state_type
        self._state = None
        self._current_state = None
        if self._client is None:
            self._state = self._state_type()
            self._current_state = self._state.clone_store()
        self._current_block_id = None

    @property
    def state(self):
        return self._current_state

    def fetch(self):
        """
        Retrieve the current state from the validator. Rebuild
        the name, type, and id maps for the resulting objects.
        """
        LOGGER.debug('fetch state from %s', self._client.base_url)

        # get the last ten block ids
        block_ids = self._client.get_block_list(10)
        block_id = block_ids[0]

        # if the latest block is the one we have.
        if block_id == self._current_block_id:
            return

        # look for the last common block.
        if self._current_block_id in block_ids:
            fetch_list = block_ids[:block_ids.index(self._current_block_id)]
            # request the updates for all the new blocks we don't have
            for fetch_id in reversed(fetch_list):
                LOGGER.debug('only fetch delta of state for block %s',
                             fetch_id)
                delta = self._client.get_store_delta_for_block(fetch_id)
                self._state = self._state.clone_store(delta)
        else:
            # no common block re-fetch full state.
            LOGGER.debug('full fetch of state for block %s', block_id)
            state = self._client.get_store_objects_through_block(block_id)
            self._state = self._state_type(prevstore=None,
                                           storeinfo={'Store': state,
                                                      'DeletedKeys': []})

        # State is actually a clone of the block state, this is a free
        # operation because of the copy on write implementation of the global
        # store. This way clients can update the state speculatively
        # without corrupting the synchronized storage
        self._current_state = self._state.clone_store()
        self._current_block_id = block_id


class UpdateBatch(object):
    """
        Helper object to allow group updates submission using
         sawtooth client.

         the block
          try:
            client.start_batch()
            client.send_txn(...)
            client.send_txn(...)
            client.send_batch()
          except:
            client.reset_batch()

         becomes:

            with UpdateBatch(client) as _:
                client.send_txn()
                client.send_txn()
    """
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        self.client.start_batch()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        if exception_type is None:
            self.client.send_batch()
        else:
            self.client.reset_batch()


class SawtoothClient(object):
    def __init__(self,
                 base_url,
                 store_name=None,
                 name='SawtoothClient',
                 transaction_type=None,
                 message_type=None,
                 keystring=None,
                 keyfile=None,
                 disable_client_validation=False):
        self._base_url = base_url
        self._message_type = message_type
        self._transaction_type = transaction_type

        # An explicit store name takes precedence over a store name
        # implied by the transaction type.
        self._store_name = None
        if store_name is not None:
            self._store_name = store_name.strip('/')
        elif transaction_type is not None:
            self._store_name = transaction_type.TransactionTypeName.strip('/')

        self._communication = _Communication(base_url)
        self._last_transaction = None
        self._local_node = None
        self._update_batch = None
        self._disable_client_validation = disable_client_validation

        # We only keep current state if we have a store name
        self._current_state = None
        if self._store_name is not None:
            state_type = global_store_manager.KeyValueStore
            if transaction_type is not None:
                state_type = transaction_type.TransactionStoreType

            self._current_state = \
                _ClientState(client=self, state_type=state_type)
            self.fetch_state()

        signing_key = None
        if keystring:
            LOGGER.debug("set signing key from string\n%s", keystring)
            signing_key = signed_object.generate_signing_key(wifstr=keystring)
        elif keyfile:
            LOGGER.debug("set signing key from file %s", keyfile)
            try:
                signing_key = signed_object.generate_signing_key(
                    wifstr=open(keyfile, "r").read().strip())
            except IOError as ex:
                raise ClientException(
                    "Failed to load key file: {}".format(str(ex)))

        if signing_key:
            identifier = signed_object.generate_identifier(signing_key)
            self._local_node = node.Node(identifier=identifier,
                                         signingkey=signing_key,
                                         name=name)

    @property
    def base_url(self):
        return self._base_url

    @property
    def state(self):
        if self._current_state is None:
            raise \
                ClientException('Client must be configured with a store name '
                                'to access its current state')

        return self._current_state.state

    @property
    def last_transaction_id(self):
        return self._last_transaction

    @staticmethod
    def _construct_store_path(txn_type_or_name=None,
                              key=None,
                              block_id=None,
                              delta=False):
        path = 'store'

        # If we are provided a transaction class or object, we will infer
        # the store name from it.  Otherwise, we will assume we have a store
        # name.
        if txn_type_or_name is not None:
            if isinstance(txn_type_or_name, transaction.Transaction):
                path += '/' + txn_type_or_name.TransactionTypeName.strip('/')
            else:
                path += '/' + txn_type_or_name.strip('/')

        if key is not None:
            path += '/' + key.strip('/')

        query = {}

        if block_id is not None:
            query['blockid'] = block_id
        if delta:
            query['delta'] = '1'
        if len(query) >= 0:
            path += '?' + urllib.urlencode(query)

        return path

    @staticmethod
    def _construct_list_path(list_type, count=None):
        path = list_type

        if count is not None:
            path += '?' + urllib.urlencode({'blockcount': int(count)})

        return path

    @staticmethod
    def _construct_block_list_path(count=0):
        return SawtoothClient._construct_list_path('block', count)

    @staticmethod
    def _construct_transaction_list_path(count=0):
        return SawtoothClient._construct_list_path('transaction', count)

    @staticmethod
    def _construct_item_path(item_type, item_id, field=None):
        path = '{0}/{1}'.format(item_type, item_id)
        if field is not None:
            path += '/' + field

        return path

    @staticmethod
    def _construct_block_path(block_id, field=None):
        return SawtoothClient._construct_item_path('block', block_id, field)

    @staticmethod
    def _construct_transaction_path(transaction_id, field=None):
        return \
            SawtoothClient._construct_item_path(
                'transaction',
                transaction_id,
                field)

    def start_batch(self):
        """
        Start a batch of updates to be sent in a single transaction to
        the validator.

        Returns:
            None

        """
        if self._update_batch is not None:
            raise ClientException(
                "Update batch already in progress.")
        self._update_batch = {
            'Updates': [],
            'Dependencies': []
        }

    def reset_batch(self):
        """
        Abandon the current batch.

        Returns:
            None
        """
        self._update_batch = None

    def send_batch(self):
        """
        Sends the current batch of transactions to the Validator.

        Returns:
            transaction_id of the update transaction

        """
        if len(self._update_batch) == 0:
            raise ClientException("No updates in batch.")
        msg_info = self._update_batch
        self._update_batch = None

        return self.sendtxn(
            minfo=msg_info,
            txn_type=self._transaction_type,
            txn_msg_type=self._message_type)

    def send_update(self, updates, dependencies=None):
        """
        Send an update or list of updates to the validator or add them to an
        existing batch.

        Args:
            updates: single update or list of updates to be sent.
            dependencies: ids of transactions dependencies.

        Returns:
            transaction_id if update is sent, None if it is added to a batch.
        """

        if self._update_batch is not None:
            # if we are in batching mode.
            if isinstance(updates, dict):  # accept single update
                self._update_batch['Updates'].append(updates)
            elif isinstance(updates, (list, tuple)):  # or a list
                self._update_batch['Updates'] += updates
            else:
                raise ClientException(
                    "Unexpected updates type {}.".format(type(updates)))
            if dependencies:
                self._update_batch['Dependencies'] += dependencies
            return None  # there is no transaction id yet.
        else:
            if isinstance(updates, dict):  # accept single update
                updates = [updates]

        dependencies = dependencies or []

        return self.sendtxn(
            minfo={
                'Updates': updates,
                'Dependencies': dependencies,
            },
            txn_type=self._transaction_type,
            txn_msg_type=self._message_type)

    def sendtxn(self, txn_type, txn_msg_type, minfo):
        """
        Build a transaction for the update, wrap it in a message with all
        of the appropriate signatures and post it to the validator
        """

        if self._local_node is None:
            raise ClientException(
                'can not send transactions as a read-only client')

        txn_type = txn_type or self._transaction_type
        txn_msg_type = txn_msg_type or self._message_type

        txn = txn_type(minfo=minfo)
        txn.sign_from_node(self._local_node)
        txnid = txn.Identifier

        if not self._disable_client_validation:
            txn.check_valid(self._current_state.state)

        msg = txn_msg_type()
        msg.Transaction = txn
        msg.SenderID = self._local_node.Identifier
        msg.sign_from_node(self._local_node)

        try:
            LOGGER.debug('Posting transaction: %s', txnid)
            result = self._communication.postmsg(msg.MessageType, msg.dump())
        except MessageException as e:
            LOGGER.warn('Posting transaction failed: %s', str(e))
            return None

        # if there was no exception thrown then all transactions should return
        # a value which is a dictionary with the message that was sent
        assert result

        # if the message was successfully posted, then save the transaction
        # id for future dependencies this could be a problem if the transaction
        # fails during application
        self._last_transaction = txnid
        if not self._disable_client_validation:
            txn.apply(self._current_state.state)
        return txnid

    def fetch_state(self):
        """
        Refresh the state for the client.

        Returns:
            Nothing
        """
        if self._current_state is None:
            raise \
                ClientException('Client must be configured with a store name '
                                'to access its current state')

        self._current_state.fetch()

    def get_state(self):
        """
        Return the most-recently-cached state for the client.  Note that this
        data may be stale and so it might be desirable to call fetch_state
        first.

        Returns:
            The most-recently-cached state for the client.
        """
        return self.state

    def get_status(self, timeout=30):
        """
        Get the status for a validator

        Args:
            timeout: Number of seconds to wait for response before determining
                reqeuest has timed out

        Returns: A dictionary of status items
        """
        return self._communication.getmsg('status', timeout)

    def get_store_list(self):
        """
        Get the list of stores on the validator.

        Returns: A list of store names
        """
        return self._communication.getmsg('store')

    def get_store_by_name(self,
                          txn_type_or_name,
                          key=None,
                          block_id=None,
                          delta=False):
        """
        Generic store retrieval method of any named store.  This allows
        complete flexibility in specifying the parameters to the HTTP
        request.

        This function is used when the client has not been configured, on
        construction, to use a specific store or you wish to access a
        different store than the object was initially configured to use.

        This function should only be used when you need absolute control
        over the HTTP request being made to the store.  Otherwise, the
        store convenience methods should be used instead.

        Args:
            txn_type_or_name: A transaction class or object (i.e., derived
                from transaction.Transaction) that can be used to infer the
                store name or a string with the store name.
            key: (optional) The object to retrieve from the store.  If None,
                will returns keys instead of objects.
            block_id: (optional) The block ID to use as ending or starting
                point of retrieval.
            delta: (optional) A flag to indicate of only a delta should be
                returned.  If key is None, this is ignored.

        Returns:
            Either a list of keys, a dictionary of name/value pairs that
            represent one or more objects, or a delta representation of the
            store.

        Notes:
            Reference the Sawtooth Lake Web API documentation for the
            behavior for the key/block_id/delta combinations.
        """
        return \
            self._communication.getmsg(
                self._construct_store_path(
                    txn_type_or_name=txn_type_or_name,
                    key=key,
                    block_id=block_id,
                    delta=delta))

    def get_store(self, key=None, block_id=None, delta=False):
        """
        Generic store retrieval method.  This allows complete flexibility
        in specifying the parameters to the HTTP request.

        This function should only be used when you need absolute control
        over the HTTP request being made to the store.  Otherwise, the
        store convenience methods should be used instead.

        Args:
            key: (optional) The object to retrieve from the store.  If None,
                will returns keys instead of objects.
            block_id: (optional) The block ID to use as ending or starting
                point of retrieval.
            delta: (optional) A flag to indicate of only a delta should be
                returned.  If key is None, this is ignored.

        Returns:
            Either a list of keys, a dictionary of name/value pairs that
            represent one or more objects, or a delta representation of the
            store.

        Notes:
            Reference the Sawtooth Lake Web API documentation for the
            behavior for the key/block_id/delta combinations.

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        if self._store_name is None:
            raise \
                ClientException(
                    'The client must be configured with a store name or '
                    'transaction type')

        return \
            self.get_store_by_name(
                txn_type_or_name=self._store_name,
                key=key,
                block_id=block_id,
                delta=delta)

    def get_store_keys(self):
        """
        Retrieve the list of keys (object IDs) from the store.

        Returns: A list of keys for the store

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        return self.get_store()

    def get_all_store_objects(self):
        """
        Retrieve all of the objects for a particular store

        Returns: A dictionary mapping object keys to objects (dictionaries
            of key/value pairs).

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        return self.get_store(key='*')

    def get_store_object_for_key(self, key):
        """
        Retrieves the object from the store corresponding to the key

        Args:
            key: The object to retrieve from the store

        Returns:
            A dictionary of name/value pairs that represent the object
            associated with the key provided.

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        return self.get_store(key=key)

    def get_store_delta_for_block(self, block_id):
        """
        Retrieves the store for just the block provided.

        Args:
            block_id: The ID of the block for which store should be returned.

        Returns:
             A dictionary that represents the store.

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        return self.get_store(key='*', block_id=block_id, delta=True)

    def get_store_objects_through_block(self, block_id):
        """
        Retrieve all of the objects for a particular store up through the
        block requested.

        Args:
            block_id: The ID of the last block to look for objects.

        Returns: A dictionary mapping object keys to objects (dictionaries
            of key/value pairs).

        Raises ClientException if the client object was not created with a
        store name or transaction type.
        """
        return self.get_store(key='*', block_id=block_id)

    def get_block_list(self, count=None):
        """
        Retrieve the list of block IDs, ordered from newest to oldest.

        Args:
            count: (optional) If not None, specifies the maximum number of
                blocks to return.

        Returns: A list of block IDs.
        """
        return \
            self._communication.getmsg(self._construct_block_list_path(count))

    def get_block(self, block_id, field=None):
        """
        Retrieve information about a specific block, returning all information
        or, if provided, a specific field from the block.

        Args:
            block_id: The ID of the block to retrieve
            field: (optional) If not None, specifies the name of the field to
                retrieve from the block.

        Returns:
            A dictionary of block data, if field is None
            The value for the field, if field is not None
        """
        return \
            self._communication.getmsg(
                self._construct_block_path(block_id, field))

    def get_transaction_list(self, block_count=None):
        """
        Retrieve the list of transaction IDs, ordered from newest to oldest.

        Args:
            block_count: (optional) If not None, specifies the maximum number
                of blocks to return transaction IDs for.

        Returns: A list of transaction IDs.
        """
        return \
            self._communication.getmsg(
                self._construct_transaction_list_path(block_count))

    def get_transaction(self, transaction_id, field=None):
        """
        Retrieve information about a specific transaction, returning all
        information or, if provided, a specific field from the transaction.

        Args:
            transaction_id: The ID of the transaction to retrieve
            field: (optional) If not None, specifies the name of the field to
                retrieve from the transaction.

        Returns:
            A dictionary of transaction data, if field is None
            The value for the field, if field is not None
        """
        return \
            self._communication.getmsg(
                self._construct_transaction_path(transaction_id, field))

    def get_transaction_status(self, transaction_id):
        """
        Retrieves that status of a transaction.

        Args:
            transaction_id: The ID of the transaction to check.

        Returns:
            One of the TransactionStatus values (committed, etc.)
        """
        return \
            self._communication.headrequest(
                self._construct_transaction_path(transaction_id))

    def forward_message(self, msg):
        """
        Post a gossip message to the ledger with the intent of having the
        receiving validator forward it to all of its peers.

        Args:
            msg: The message to send.

        Returns: The parsed response, which is typically the encoding of
            the original message.
        """
        return self._communication.postmsg('forward', msg.dump())

    def wait_for_commit(self, txnid=None, timetowait=5, iterations=12):
        """
        Wait until a specified transaction shows up in the ledger's committed
        transaction list

        :param id txnid: the transaction to wait for, the last transaction by
            default
        :param int timetowait: time to wait between polling the ledger
        :param int iterations: number of iterations to wait before giving up
        """

        if not txnid:
            txnid = self._last_transaction
        if not txnid:
            LOGGER.info('no transaction specified for wait')
            return True

        start_time = time.time()
        passes = 0
        while True:
            passes += 1
            status = self.get_transaction_status(txnid)
            if status != TransactionStatus.committed and passes > iterations:
                if status == TransactionStatus.not_found:
                    LOGGER.warn('unknown transaction %s', txnid)
                elif status == TransactionStatus.pending:
                    LOGGER.warn(
                        'transaction %s still uncommitted after %d sec',
                        txnid, int(time.time() - start_time))
                else:
                    LOGGER.warn(
                        'transaction %s returned unexpected status code %d',
                        txnid, status)
                return False

            if status == TransactionStatus.committed:
                return True

            try:
                pretty_status = "{}:{}".format(
                    TransactionStatus(status).name, status)
            except ValueError:
                pretty_status = str(status)

            LOGGER.debug(
                'waiting for transaction %s to commit (%s)',
                txnid,
                pretty_status)
            time.sleep(timetowait)
