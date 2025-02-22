# Copyright (c) 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
import shutil
import tempfile
import unittest

import eventlet
import mock
import six

from swift.common import bufferedhttp
from swift.common import exceptions
from swift.common import swob
from swift.common.storage_policy import POLICIES
from swift.common import utils
from swift.common.swob import HTTPException
from swift.obj import diskfile
from swift.obj import server
from swift.obj import ssync_receiver, ssync_sender
from swift.obj.reconstructor import ObjectReconstructor

from test import unit
from test.unit import debug_logger, patch_policies


@unit.patch_policies()
class TestReceiver(unittest.TestCase):

    def setUp(self):
        utils.HASH_PATH_SUFFIX = 'endcap'
        utils.HASH_PATH_PREFIX = 'startcap'
        # Not sure why the test.unit stuff isn't taking effect here; so I'm
        # reinforcing it.
        diskfile.getxattr = unit._getxattr
        diskfile.setxattr = unit._setxattr
        self.testdir = os.path.join(
            tempfile.mkdtemp(), 'tmp_test_ssync_receiver')
        utils.mkdirs(os.path.join(self.testdir, 'sda1', 'tmp'))
        self.conf = {
            'devices': self.testdir,
            'mount_check': 'false',
            'replication_one_per_device': 'false',
            'log_requests': 'false'}
        utils.mkdirs(os.path.join(self.testdir, 'device', 'partition'))
        self.controller = server.ObjectController(self.conf)
        self.controller.bytes_per_sync = 1

        self.account1 = 'a'
        self.container1 = 'c'
        self.object1 = 'o1'
        self.name1 = '/' + '/'.join((
            self.account1, self.container1, self.object1))
        self.hash1 = utils.hash_path(
            self.account1, self.container1, self.object1)
        self.ts1 = '1372800001.00000'
        self.metadata1 = {
            'name': self.name1,
            'X-Timestamp': self.ts1,
            'Content-Length': '0'}

        self.account2 = 'a'
        self.container2 = 'c'
        self.object2 = 'o2'
        self.name2 = '/' + '/'.join((
            self.account2, self.container2, self.object2))
        self.hash2 = utils.hash_path(
            self.account2, self.container2, self.object2)
        self.ts2 = '1372800002.00000'
        self.metadata2 = {
            'name': self.name2,
            'X-Timestamp': self.ts2,
            'Content-Length': '0'}

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.testdir))

    def body_lines(self, body):
        lines = []
        for line in body.split('\n'):
            line = line.strip()
            if line:
                lines.append(line)
        return lines

    def test_SSYNC_semaphore_locked(self):
        with mock.patch.object(
                self.controller, 'replication_semaphore') as \
                mocked_replication_semaphore:
            self.controller.logger = mock.MagicMock()
            mocked_replication_semaphore.acquire.return_value = False
            req = swob.Request.blank(
                '/device/partition', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [":ERROR: 503 '<html><h1>Service Unavailable</h1><p>The "
                 "server is currently unavailable. Please try again at a "
                 "later time.</p></html>'"])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.error.called)
            self.assertFalse(self.controller.logger.exception.called)

    def test_SSYNC_calls_replication_lock(self):
        with mock.patch.object(
                self.controller._diskfile_router[POLICIES.legacy],
                'replication_lock') as mocked_replication_lock:
            req = swob.Request.blank(
                '/sda1/1',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n'
                     ':MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n:UPDATES: END\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            mocked_replication_lock.assert_called_once_with('sda1')

    def test_Receiver_with_default_storage_policy(self):
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        rcvr = ssync_receiver.Receiver(self.controller, req)
        body_lines = [chunk.strip() for chunk in rcvr() if chunk.strip()]
        self.assertEqual(
            body_lines,
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(rcvr.policy, POLICIES[0])

    def test_Receiver_with_storage_policy_index_header(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        rcvr = ssync_receiver.Receiver(self.controller, req)
        body_lines = [chunk.strip() for chunk in rcvr() if chunk.strip()]
        self.assertEqual(
            body_lines,
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(rcvr.policy, POLICIES[1])
        self.assertEqual(rcvr.frag_index, None)

    def test_Receiver_with_bad_storage_policy_index_header(self):
        valid_indices = sorted([int(policy) for policy in POLICIES])
        bad_index = valid_indices[-1] + 1
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_SSYNC_FRAG_INDEX': '0',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': bad_index},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        self.controller.logger = mock.MagicMock()
        try:
            ssync_receiver.Receiver(self.controller, req)
            self.fail('Expected HTTPException to be raised.')
        except HTTPException as err:
            self.assertEqual('503 Service Unavailable', err.status)
            self.assertEqual('No policy with index 2', err.body)

    @unit.patch_policies()
    def test_Receiver_with_only_frag_index_header(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_SSYNC_FRAG_INDEX': '7',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        rcvr = ssync_receiver.Receiver(self.controller, req)
        body_lines = [chunk.strip() for chunk in rcvr() if chunk.strip()]
        self.assertEqual(
            body_lines,
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(rcvr.policy, POLICIES[1])
        self.assertEqual(rcvr.frag_index, 7)
        self.assertEqual(rcvr.node_index, None)

    @unit.patch_policies()
    def test_Receiver_with_only_node_index_header(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_SSYNC_NODE_INDEX': '7',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        with self.assertRaises(HTTPException) as e:
            ssync_receiver.Receiver(self.controller, req)
        self.assertEqual(e.exception.status_int, 400)
        # if a node index is included - it *must* be
        # the same value of frag index
        self.assertEqual(e.exception.body,
                         'Frag-Index (None) != Node-Index (7)')

    @unit.patch_policies()
    def test_Receiver_with_matched_indexes(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_SSYNC_NODE_INDEX': '7',
                     'HTTP_X_BACKEND_SSYNC_FRAG_INDEX': '7',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        rcvr = ssync_receiver.Receiver(self.controller, req)
        body_lines = [chunk.strip() for chunk in rcvr() if chunk.strip()]
        self.assertEqual(
            body_lines,
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(rcvr.policy, POLICIES[1])
        self.assertEqual(rcvr.frag_index, 7)
        self.assertEqual(rcvr.node_index, 7)

    @unit.patch_policies()
    def test_Receiver_with_mismatched_indexes(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_SSYNC_NODE_INDEX': '6',
                     'HTTP_X_BACKEND_SSYNC_FRAG_INDEX': '7',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        self.assertRaises(HTTPException, ssync_receiver.Receiver,
                          self.controller, req)

    def test_SSYNC_replication_lock_fail(self):
        def _mock(path):
            with exceptions.ReplicationLockTimeout(0.01, '/somewhere/' + path):
                eventlet.sleep(0.05)
        with mock.patch.object(
                self.controller._diskfile_router[POLICIES.legacy],
                'replication_lock', _mock):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/sda1/1',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n'
                     ':MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n:UPDATES: END\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [":ERROR: 0 '0.01 seconds: /somewhere/sda1'"])
            self.controller.logger.debug.assert_called_once_with(
                'None/sda1/1 SSYNC LOCK TIMEOUT: 0.01 seconds: '
                '/somewhere/sda1')

    def test_SSYNC_initial_path(self):
        with mock.patch.object(
                self.controller, 'replication_semaphore') as \
                mocked_replication_semaphore:
            req = swob.Request.blank(
                '/device', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                ["Invalid path: /device"])
            self.assertEqual(resp.status_int, 400)
            self.assertFalse(mocked_replication_semaphore.acquire.called)
            self.assertFalse(mocked_replication_semaphore.release.called)

        with mock.patch.object(
                self.controller, 'replication_semaphore') as \
                mocked_replication_semaphore:
            req = swob.Request.blank(
                '/device/', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                ["Invalid path: /device/"])
            self.assertEqual(resp.status_int, 400)
            self.assertFalse(mocked_replication_semaphore.acquire.called)
            self.assertFalse(mocked_replication_semaphore.release.called)

        with mock.patch.object(
                self.controller, 'replication_semaphore') as \
                mocked_replication_semaphore:
            req = swob.Request.blank(
                '/device/partition', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':ERROR: 0 "Looking for :MISSING_CHECK: START got \'\'"'])
            self.assertEqual(resp.status_int, 200)
            mocked_replication_semaphore.acquire.assert_called_once_with(0)
            mocked_replication_semaphore.release.assert_called_once_with()

        with mock.patch.object(
                self.controller, 'replication_semaphore') as \
                mocked_replication_semaphore:
            req = swob.Request.blank(
                '/device/partition/junk',
                environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                ["Invalid path: /device/partition/junk"])
            self.assertEqual(resp.status_int, 400)
            self.assertFalse(mocked_replication_semaphore.acquire.called)
            self.assertFalse(mocked_replication_semaphore.release.called)

    def test_SSYNC_mount_check(self):
        with contextlib.nested(
                mock.patch.object(
                    self.controller, 'replication_semaphore'),
                mock.patch.object(
                    self.controller._diskfile_router[POLICIES.legacy],
                    'mount_check', False),
                mock.patch('swift.obj.diskfile.check_mount',
                           return_value=False)) as (
                mocked_replication_semaphore,
                mocked_mount_check,
                mocked_check_mount):
            req = swob.Request.blank(
                '/device/partition', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':ERROR: 0 "Looking for :MISSING_CHECK: START got \'\'"'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(mocked_check_mount.called)

        with contextlib.nested(
                mock.patch.object(
                    self.controller, 'replication_semaphore'),
                mock.patch.object(
                    self.controller._diskfile_router[POLICIES.legacy],
                    'mount_check', True),
                mock.patch('swift.obj.diskfile.check_mount',
                           return_value=False)) as (
                mocked_replication_semaphore,
                mocked_mount_check,
                mocked_check_mount):
            req = swob.Request.blank(
                '/device/partition', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                ["<html><h1>Insufficient Storage</h1><p>There "
                 "was not enough space to save the resource. Drive: "
                 "device</p></html>"])
            self.assertEqual(resp.status_int, 507)
            mocked_check_mount.assert_called_once_with(
                self.controller._diskfile_router[POLICIES.legacy].devices,
                'device')

            mocked_check_mount.reset_mock()
            mocked_check_mount.return_value = True
            req = swob.Request.blank(
                '/device/partition', environ={'REQUEST_METHOD': 'SSYNC'})
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':ERROR: 0 "Looking for :MISSING_CHECK: START got \'\'"'])
            self.assertEqual(resp.status_int, 200)
            mocked_check_mount.assert_called_once_with(
                self.controller._diskfile_router[POLICIES.legacy].devices,
                'device')

    def test_SSYNC_Exception(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def get_socket(self):
                return self.mock_socket

        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\nBad content is here')
            req.remote_addr = '1.2.3.4'
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'Got no headers for Bad content is here'"])
            self.assertEqual(resp.status_int, 200)
            mock_shutdown_safe.assert_called_once_with(
                mock_wsgi_input.mock_socket)
            mock_wsgi_input.mock_socket.close.assert_called_once_with()
            self.controller.logger.exception.assert_called_once_with(
                '1.2.3.4/device/partition EXCEPTION in replication.Receiver')

    def test_SSYNC_Exception_Exception(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def get_socket(self):
                return self.mock_socket

        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\nBad content is here')
            req.remote_addr = mock.MagicMock()
            req.remote_addr.__str__ = mock.Mock(
                side_effect=Exception("can't stringify this"))
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END'])
            self.assertEqual(resp.status_int, 200)
            mock_shutdown_safe.assert_called_once_with(
                mock_wsgi_input.mock_socket)
            mock_wsgi_input.mock_socket.close.assert_called_once_with()
            self.controller.logger.exception.assert_called_once_with(
                'EXCEPTION in replication.Receiver')

    def test_MISSING_CHECK_timeout(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def readline(self, sizehint=-1):
                line = six.StringIO.readline(self)
                if line.startswith('hash'):
                    eventlet.sleep(0.1)
                return line

            def get_socket(self):
                return self.mock_socket

        self.controller.client_timeout = 0.01
        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/sda1/1',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n'
                     'hash ts\r\n'
                     ':MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n:UPDATES: END\r\n')
            req.remote_addr = '2.3.4.5'
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [":ERROR: 408 '0.01 seconds: missing_check line'"])
            self.assertEqual(resp.status_int, 200)
            self.assertTrue(mock_shutdown_safe.called)
            self.controller.logger.error.assert_called_once_with(
                '2.3.4.5/sda1/1 TIMEOUT in replication.Receiver: '
                '0.01 seconds: missing_check line')

    def test_MISSING_CHECK_other_exception(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def readline(self, sizehint=-1):
                line = six.StringIO.readline(self)
                if line.startswith('hash'):
                    raise Exception('test exception')
                return line

            def get_socket(self):
                return self.mock_socket

        self.controller.client_timeout = 0.01
        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/sda1/1',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n'
                     'hash ts\r\n'
                     ':MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n:UPDATES: END\r\n')
            req.remote_addr = '3.4.5.6'
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [":ERROR: 0 'test exception'"])
            self.assertEqual(resp.status_int, 200)
            self.assertTrue(mock_shutdown_safe.called)
            self.controller.logger.exception.assert_called_once_with(
                '3.4.5.6/sda1/1 EXCEPTION in replication.Receiver')

    def test_MISSING_CHECK_empty_list(self):

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_have_none(self):

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash1,
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_extra_line_parts(self):
        # check that rx tolerates extra parts in missing check lines to
        # allow for protocol upgrades
        extra_1 = 'extra'
        extra_2 = 'multiple extra parts'
        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + ' ' + extra_1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + ' ' + extra_2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash1,
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_have_one_exact(self):
        object_dir = utils.storage_directory(
            os.path.join(self.testdir, 'sda1',
                         diskfile.get_data_dir(POLICIES[0])),
            '1', self.hash1)
        utils.mkdirs(object_dir)
        fp = open(os.path.join(object_dir, self.ts1 + '.data'), 'w+')
        fp.write('1')
        fp.flush()
        self.metadata1['Content-Length'] = '1'
        diskfile.write_metadata(fp, self.metadata1)

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_storage_policy(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        object_dir = utils.storage_directory(
            os.path.join(self.testdir, 'sda1',
                         diskfile.get_data_dir(POLICIES[1])),
            '1', self.hash1)
        utils.mkdirs(object_dir)
        fp = open(os.path.join(object_dir, self.ts1 + '.data'), 'w+')
        fp.write('1')
        fp.flush()
        self.metadata1['Content-Length'] = '1'
        diskfile.write_metadata(fp, self.metadata1)

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC',
                     'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_have_one_newer(self):
        object_dir = utils.storage_directory(
            os.path.join(self.testdir, 'sda1',
                         diskfile.get_data_dir(POLICIES[0])),
            '1', self.hash1)
        utils.mkdirs(object_dir)
        newer_ts1 = utils.normalize_timestamp(float(self.ts1) + 1)
        self.metadata1['X-Timestamp'] = newer_ts1
        fp = open(os.path.join(object_dir, newer_ts1 + '.data'), 'w+')
        fp.write('1')
        fp.flush()
        self.metadata1['Content-Length'] = '1'
        diskfile.write_metadata(fp, self.metadata1)

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_MISSING_CHECK_have_one_older(self):
        object_dir = utils.storage_directory(
            os.path.join(self.testdir, 'sda1',
                         diskfile.get_data_dir(POLICIES[0])),
            '1', self.hash1)
        utils.mkdirs(object_dir)
        older_ts1 = utils.normalize_timestamp(float(self.ts1) - 1)
        self.metadata1['X-Timestamp'] = older_ts1
        fp = open(os.path.join(object_dir, older_ts1 + '.data'), 'w+')
        fp.write('1')
        fp.flush()
        self.metadata1['Content-Length'] = '1'
        diskfile.write_metadata(fp, self.metadata1)

        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/sda1/1',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n' +
                 self.hash1 + ' ' + self.ts1 + '\r\n' +
                 self.hash2 + ' ' + self.ts2 + '\r\n'
                 ':MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n:UPDATES: END\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START',
             self.hash1,
             self.hash2,
             ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.error.called)
        self.assertFalse(self.controller.logger.exception.called)

    def test_UPDATES_timeout(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def readline(self, sizehint=-1):
                line = six.StringIO.readline(self)
                if line.startswith('DELETE'):
                    eventlet.sleep(0.1)
                return line

            def get_socket(self):
                return self.mock_socket

        self.controller.client_timeout = 0.01
        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'X-Timestamp: 1364456113.76334\r\n'
                     '\r\n'
                     ':UPDATES: END\r\n')
            req.remote_addr = '2.3.4.5'
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 408 '0.01 seconds: updates line'"])
            self.assertEqual(resp.status_int, 200)
            mock_shutdown_safe.assert_called_once_with(
                mock_wsgi_input.mock_socket)
            mock_wsgi_input.mock_socket.close.assert_called_once_with()
            self.controller.logger.error.assert_called_once_with(
                '2.3.4.5/device/partition TIMEOUT in replication.Receiver: '
                '0.01 seconds: updates line')

    def test_UPDATES_other_exception(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def readline(self, sizehint=-1):
                line = six.StringIO.readline(self)
                if line.startswith('DELETE'):
                    raise Exception('test exception')
                return line

            def get_socket(self):
                return self.mock_socket

        self.controller.client_timeout = 0.01
        with mock.patch.object(
                ssync_receiver.eventlet.greenio, 'shutdown_safe') as \
                mock_shutdown_safe:
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'X-Timestamp: 1364456113.76334\r\n'
                     '\r\n'
                     ':UPDATES: END\r\n')
            req.remote_addr = '3.4.5.6'
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'test exception'"])
            self.assertEqual(resp.status_int, 200)
            mock_shutdown_safe.assert_called_once_with(
                mock_wsgi_input.mock_socket)
            mock_wsgi_input.mock_socket.close.assert_called_once_with()
            self.controller.logger.exception.assert_called_once_with(
                '3.4.5.6/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_no_problems_no_hard_disconnect(self):

        class _Wrapper(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)
                self.mock_socket = mock.MagicMock()

            def get_socket(self):
                return self.mock_socket

        self.controller.client_timeout = 0.01
        with contextlib.nested(
                mock.patch.object(
                    ssync_receiver.eventlet.greenio, 'shutdown_safe'),
                mock.patch.object(
                    self.controller, 'DELETE',
                    return_value=swob.HTTPNoContent())) as (
                mock_shutdown_safe, mock_delete):
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'X-Timestamp: 1364456113.76334\r\n'
                     '\r\n'
                     ':UPDATES: END\r\n')
            mock_wsgi_input = _Wrapper(req.body)
            req.environ['wsgi.input'] = mock_wsgi_input
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(mock_shutdown_safe.called)
            self.assertFalse(mock_wsgi_input.mock_socket.close.called)

    def test_UPDATES_bad_subrequest_line(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'bad_subrequest_line\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'need more than 1 value to unpack'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

            with mock.patch.object(
                    self.controller, 'DELETE',
                    return_value=swob.HTTPNoContent()):
                self.controller.logger = mock.MagicMock()
                req = swob.Request.blank(
                    '/device/partition',
                    environ={'REQUEST_METHOD': 'SSYNC'},
                    body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                         ':UPDATES: START\r\n'
                         'DELETE /a/c/o\r\n'
                         'X-Timestamp: 1364456113.76334\r\n'
                         '\r\n'
                         'bad_subrequest_line2')
                resp = req.get_response(self.controller)
                self.assertEqual(
                    self.body_lines(resp.body),
                    [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                     ":ERROR: 0 'need more than 1 value to unpack'"])
                self.assertEqual(resp.status_int, 200)
                self.controller.logger.exception.assert_called_once_with(
                    'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_no_headers(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'Got no headers for DELETE /a/c/o'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_bad_headers(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'Bad-Header Test\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'need more than 1 value to unpack'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'Good-Header: Test\r\n'
                     'Bad-Header Test\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'need more than 1 value to unpack'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_bad_content_length(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n'
                     'Content-Length: a\r\n\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':ERROR: 0 "invalid literal for int() with base 10: \'a\'"'])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_content_length_with_DELETE(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'Content-Length: 1\r\n\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'DELETE subrequest with content-length /a/c/o'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_no_content_length_with_PUT(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'No content-length sent for PUT /a/c/o'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_early_termination(self):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n'
                     'Content-Length: 1\r\n\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'Early termination for PUT /a/c/o'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')

    def test_UPDATES_failures(self):

        @server.public
        def _DELETE(request):
            if request.path == '/device/partition/a/c/works':
                return swob.HTTPNoContent()
            else:
                return swob.HTTPInternalServerError()

        # failures never hit threshold
        with mock.patch.object(self.controller, 'DELETE', _DELETE):
            self.controller.replication_failure_threshold = 4
            self.controller.replication_failure_ratio = 1.5
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 500 'ERROR: With :UPDATES: 3 failures to 0 "
                 "successes'"])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)

        # failures hit threshold and no successes, so ratio is like infinity
        with mock.patch.object(self.controller, 'DELETE', _DELETE):
            self.controller.replication_failure_threshold = 4
            self.controller.replication_failure_ratio = 1.5
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     ':UPDATES: END\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'Too many 4 failures to 0 successes'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')
            self.assertFalse(self.controller.logger.error.called)

        # failures hit threshold and ratio hits 1.33333333333
        with mock.patch.object(self.controller, 'DELETE', _DELETE):
            self.controller.replication_failure_threshold = 4
            self.controller.replication_failure_ratio = 1.5
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/works\r\n\r\n'
                     'DELETE /a/c/works\r\n\r\n'
                     'DELETE /a/c/works\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     ':UPDATES: END\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 500 'ERROR: With :UPDATES: 4 failures to 3 "
                 "successes'"])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)

        # failures hit threshold and ratio hits 2.0
        with mock.patch.object(self.controller, 'DELETE', _DELETE):
            self.controller.replication_failure_threshold = 4
            self.controller.replication_failure_ratio = 1.5
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/works\r\n\r\n'
                     'DELETE /a/c/works\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     'DELETE /a/c/o\r\n\r\n'
                     ':UPDATES: END\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ":ERROR: 0 'Too many 4 failures to 2 successes'"])
            self.assertEqual(resp.status_int, 200)
            self.controller.logger.exception.assert_called_once_with(
                'None/device/partition EXCEPTION in replication.Receiver')
            self.assertFalse(self.controller.logger.error.called)

    def test_UPDATES_PUT(self):
        _PUT_request = [None]

        @server.public
        def _PUT(request):
            _PUT_request[0] = request
            request.read_body = request.environ['wsgi.input'].read()
            return swob.HTTPCreated()

        with mock.patch.object(self.controller, 'PUT', _PUT):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n'
                     'Content-Length: 1\r\n'
                     'Etag: c4ca4238a0b923820dcc509a6f75849b\r\n'
                     'X-Timestamp: 1364456113.12344\r\n'
                     'X-Object-Meta-Test1: one\r\n'
                     'Content-Encoding: gzip\r\n'
                     'Specialty-Header: value\r\n'
                     '\r\n'
                     '1')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)
            self.assertEqual(len(_PUT_request), 1)  # sanity
            req = _PUT_request[0]
            self.assertEqual(req.path, '/device/partition/a/c/o')
            self.assertEqual(req.content_length, 1)
            self.assertEqual(req.headers, {
                'Etag': 'c4ca4238a0b923820dcc509a6f75849b',
                'Content-Length': '1',
                'X-Timestamp': '1364456113.12344',
                'X-Object-Meta-Test1': 'one',
                'Content-Encoding': 'gzip',
                'Specialty-Header': 'value',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp x-object-meta-test1 '
                    'content-encoding specialty-header')})

    def test_UPDATES_PUT_replication_headers(self):
        self.controller.logger = mock.MagicMock()

        # sanity check - regular PUT will not persist Specialty-Header
        req = swob.Request.blank(
            '/sda1/0/a/c/o1', body='1',
            environ={'REQUEST_METHOD': 'PUT'},
            headers={'Content-Length': '1',
                     'Content-Type': 'text/plain',
                     'Etag': 'c4ca4238a0b923820dcc509a6f75849b',
                     'X-Timestamp': '1364456113.12344',
                     'X-Object-Meta-Test1': 'one',
                     'Content-Encoding': 'gzip',
                     'Specialty-Header': 'value'})
        resp = req.get_response(self.controller)
        self.assertEqual(resp.status_int, 201)
        df = self.controller.get_diskfile(
            'sda1', '0', 'a', 'c', 'o1', POLICIES.default)
        df.open()
        self.assertFalse('Specialty-Header' in df.get_metadata())

        # an SSYNC request can override PUT header filtering...
        req = swob.Request.blank(
            '/sda1/0',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n'
                 'PUT /a/c/o2\r\n'
                 'Content-Length: 1\r\n'
                 'Content-Type: text/plain\r\n'
                 'Etag: c4ca4238a0b923820dcc509a6f75849b\r\n'
                 'X-Timestamp: 1364456113.12344\r\n'
                 'X-Object-Meta-Test1: one\r\n'
                 'Content-Encoding: gzip\r\n'
                 'Specialty-Header: value\r\n'
                 '\r\n'
                 '1')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ':UPDATES: START', ':UPDATES: END'])
        self.assertEqual(resp.status_int, 200)

        # verify diskfile has metadata permitted by replication headers
        # including Specialty-Header
        df = self.controller.get_diskfile(
            'sda1', '0', 'a', 'c', 'o2', POLICIES.default)
        df.open()
        for chunk in df.reader():
            self.assertEqual('1', chunk)
        expected = {'ETag': 'c4ca4238a0b923820dcc509a6f75849b',
                    'Content-Length': '1',
                    'Content-Type': 'text/plain',
                    'X-Timestamp': '1364456113.12344',
                    'X-Object-Meta-Test1': 'one',
                    'Content-Encoding': 'gzip',
                    'Specialty-Header': 'value',
                    'name': '/a/c/o2'}
        actual = df.get_metadata()
        self.assertEqual(expected, actual)

    def test_UPDATES_with_storage_policy(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)
        _PUT_request = [None]

        @server.public
        def _PUT(request):
            _PUT_request[0] = request
            request.read_body = request.environ['wsgi.input'].read()
            return swob.HTTPCreated()

        with mock.patch.object(self.controller, 'PUT', _PUT):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC',
                         'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '1'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n'
                     'Content-Length: 1\r\n'
                     'X-Timestamp: 1364456113.12344\r\n'
                     'X-Object-Meta-Test1: one\r\n'
                     'Content-Encoding: gzip\r\n'
                     'Specialty-Header: value\r\n'
                     '\r\n'
                     '1')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)
            self.assertEqual(len(_PUT_request), 1)  # sanity
            req = _PUT_request[0]
            self.assertEqual(req.path, '/device/partition/a/c/o')
            self.assertEqual(req.content_length, 1)
            self.assertEqual(req.headers, {
                'Content-Length': '1',
                'X-Timestamp': '1364456113.12344',
                'X-Object-Meta-Test1': 'one',
                'Content-Encoding': 'gzip',
                'Specialty-Header': 'value',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '1',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp x-object-meta-test1 '
                    'content-encoding specialty-header')})
            self.assertEqual(req.read_body, '1')

    def test_UPDATES_PUT_with_storage_policy_and_node_index(self):
        # update router post policy patch
        self.controller._diskfile_router = diskfile.DiskFileRouter(
            self.conf, self.controller.logger)

        _PUT_request = [None]

        @server.public
        def _PUT(request):
            _PUT_request[0] = request
            request.read_body = request.environ['wsgi.input'].read()
            return swob.HTTPCreated()

        with mock.patch.object(self.controller, 'PUT', _PUT):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC',
                         'HTTP_X_BACKEND_SSYNC_NODE_INDEX': '7',
                         'HTTP_X_BACKEND_SSYNC_FRAG_INDEX': '7',
                         'HTTP_X_BACKEND_STORAGE_POLICY_INDEX': '0'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o\r\n'
                     'Content-Length: 1\r\n'
                     'X-Timestamp: 1364456113.12344\r\n'
                     'X-Object-Meta-Test1: one\r\n'
                     'Content-Encoding: gzip\r\n'
                     'Specialty-Header: value\r\n'
                     '\r\n'
                     '1')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)
            self.assertEqual(len(_PUT_request), 1)  # sanity
            req = _PUT_request[0]
            self.assertEqual(req.path, '/device/partition/a/c/o')
            self.assertEqual(req.content_length, 1)
            self.assertEqual(req.headers, {
                'Content-Length': '1',
                'X-Timestamp': '1364456113.12344',
                'X-Object-Meta-Test1': 'one',
                'Content-Encoding': 'gzip',
                'Specialty-Header': 'value',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Ssync-Frag-Index': '7',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp x-object-meta-test1 '
                    'content-encoding specialty-header')})
            self.assertEqual(req.read_body, '1')

    def test_UPDATES_DELETE(self):
        _DELETE_request = [None]

        @server.public
        def _DELETE(request):
            _DELETE_request[0] = request
            return swob.HTTPNoContent()

        with mock.patch.object(self.controller, 'DELETE', _DELETE):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'DELETE /a/c/o\r\n'
                     'X-Timestamp: 1364456113.76334\r\n'
                     '\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)
            self.assertEqual(len(_DELETE_request), 1)  # sanity
            req = _DELETE_request[0]
            self.assertEqual(req.path, '/device/partition/a/c/o')
            self.assertEqual(req.headers, {
                'X-Timestamp': '1364456113.76334',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': 'x-timestamp'})

    def test_UPDATES_BONK(self):
        _BONK_request = [None]

        @server.public
        def _BONK(request):
            _BONK_request[0] = request
            return swob.HTTPOk()

        self.controller.BONK = _BONK
        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/device/partition',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n'
                 'BONK /a/c/o\r\n'
                 'X-Timestamp: 1364456113.76334\r\n'
                 '\r\n')
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ":ERROR: 0 'Invalid subrequest method BONK'"])
        self.assertEqual(resp.status_int, 200)
        self.controller.logger.exception.assert_called_once_with(
            'None/device/partition EXCEPTION in replication.Receiver')
        self.assertEqual(len(_BONK_request), 1)  # sanity
        self.assertEqual(_BONK_request[0], None)

    def test_UPDATES_multiple(self):
        _requests = []

        @server.public
        def _PUT(request):
            _requests.append(request)
            request.read_body = request.environ['wsgi.input'].read()
            return swob.HTTPCreated()

        @server.public
        def _DELETE(request):
            _requests.append(request)
            return swob.HTTPNoContent()

        with contextlib.nested(
                mock.patch.object(self.controller, 'PUT', _PUT),
                mock.patch.object(self.controller, 'DELETE', _DELETE)):
            self.controller.logger = mock.MagicMock()
            req = swob.Request.blank(
                '/device/partition',
                environ={'REQUEST_METHOD': 'SSYNC'},
                body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                     ':UPDATES: START\r\n'
                     'PUT /a/c/o1\r\n'
                     'Content-Length: 1\r\n'
                     'X-Timestamp: 1364456113.00001\r\n'
                     'X-Object-Meta-Test1: one\r\n'
                     'Content-Encoding: gzip\r\n'
                     'Specialty-Header: value\r\n'
                     '\r\n'
                     '1'
                     'DELETE /a/c/o2\r\n'
                     'X-Timestamp: 1364456113.00002\r\n'
                     '\r\n'
                     'PUT /a/c/o3\r\n'
                     'Content-Length: 3\r\n'
                     'X-Timestamp: 1364456113.00003\r\n'
                     '\r\n'
                     '123'
                     'PUT /a/c/o4\r\n'
                     'Content-Length: 4\r\n'
                     'X-Timestamp: 1364456113.00004\r\n'
                     '\r\n'
                     '1\r\n4'
                     'DELETE /a/c/o5\r\n'
                     'X-Timestamp: 1364456113.00005\r\n'
                     '\r\n'
                     'DELETE /a/c/o6\r\n'
                     'X-Timestamp: 1364456113.00006\r\n'
                     '\r\n')
            resp = req.get_response(self.controller)
            self.assertEqual(
                self.body_lines(resp.body),
                [':MISSING_CHECK: START', ':MISSING_CHECK: END',
                 ':UPDATES: START', ':UPDATES: END'])
            self.assertEqual(resp.status_int, 200)
            self.assertFalse(self.controller.logger.exception.called)
            self.assertFalse(self.controller.logger.error.called)
            self.assertEqual(len(_requests), 6)  # sanity
            req = _requests.pop(0)
            self.assertEqual(req.method, 'PUT')
            self.assertEqual(req.path, '/device/partition/a/c/o1')
            self.assertEqual(req.content_length, 1)
            self.assertEqual(req.headers, {
                'Content-Length': '1',
                'X-Timestamp': '1364456113.00001',
                'X-Object-Meta-Test1': 'one',
                'Content-Encoding': 'gzip',
                'Specialty-Header': 'value',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp x-object-meta-test1 '
                    'content-encoding specialty-header')})
            self.assertEqual(req.read_body, '1')
            req = _requests.pop(0)
            self.assertEqual(req.method, 'DELETE')
            self.assertEqual(req.path, '/device/partition/a/c/o2')
            self.assertEqual(req.headers, {
                'X-Timestamp': '1364456113.00002',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': 'x-timestamp'})
            req = _requests.pop(0)
            self.assertEqual(req.method, 'PUT')
            self.assertEqual(req.path, '/device/partition/a/c/o3')
            self.assertEqual(req.content_length, 3)
            self.assertEqual(req.headers, {
                'Content-Length': '3',
                'X-Timestamp': '1364456113.00003',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp')})
            self.assertEqual(req.read_body, '123')
            req = _requests.pop(0)
            self.assertEqual(req.method, 'PUT')
            self.assertEqual(req.path, '/device/partition/a/c/o4')
            self.assertEqual(req.content_length, 4)
            self.assertEqual(req.headers, {
                'Content-Length': '4',
                'X-Timestamp': '1364456113.00004',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': (
                    'content-length x-timestamp')})
            self.assertEqual(req.read_body, '1\r\n4')
            req = _requests.pop(0)
            self.assertEqual(req.method, 'DELETE')
            self.assertEqual(req.path, '/device/partition/a/c/o5')
            self.assertEqual(req.headers, {
                'X-Timestamp': '1364456113.00005',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': 'x-timestamp'})
            req = _requests.pop(0)
            self.assertEqual(req.method, 'DELETE')
            self.assertEqual(req.path, '/device/partition/a/c/o6')
            self.assertEqual(req.headers, {
                'X-Timestamp': '1364456113.00006',
                'Host': 'localhost:80',
                'X-Backend-Storage-Policy-Index': '0',
                'X-Backend-Replication': 'True',
                'X-Backend-Replication-Headers': 'x-timestamp'})
            self.assertEqual(_requests, [])

    def test_UPDATES_subreq_does_not_read_all(self):
        # This tests that if a SSYNC subrequest fails and doesn't read
        # all the subrequest body that it will read and throw away the rest of
        # the body before moving on to the next subrequest.
        # If you comment out the part in ssync_receiver where it does:
        #     for junk in subreq.environ['wsgi.input']:
        #         pass
        # You can then see this test fail.
        _requests = []

        @server.public
        def _PUT(request):
            _requests.append(request)
            # Deliberately just reading up to first 2 bytes.
            request.read_body = request.environ['wsgi.input'].read(2)
            return swob.HTTPInternalServerError()

        class _IgnoreReadlineHint(six.StringIO):

            def __init__(self, value):
                six.StringIO.__init__(self, value)

            def readline(self, hint=-1):
                return six.StringIO.readline(self)

        self.controller.PUT = _PUT
        self.controller.network_chunk_size = 2
        self.controller.logger = mock.MagicMock()
        req = swob.Request.blank(
            '/device/partition',
            environ={'REQUEST_METHOD': 'SSYNC'},
            body=':MISSING_CHECK: START\r\n:MISSING_CHECK: END\r\n'
                 ':UPDATES: START\r\n'
                 'PUT /a/c/o1\r\n'
                 'Content-Length: 3\r\n'
                 'X-Timestamp: 1364456113.00001\r\n'
                 '\r\n'
                 '123'
                 'PUT /a/c/o2\r\n'
                 'Content-Length: 1\r\n'
                 'X-Timestamp: 1364456113.00002\r\n'
                 '\r\n'
                 '1')
        req.environ['wsgi.input'] = _IgnoreReadlineHint(req.body)
        resp = req.get_response(self.controller)
        self.assertEqual(
            self.body_lines(resp.body),
            [':MISSING_CHECK: START', ':MISSING_CHECK: END',
             ":ERROR: 500 'ERROR: With :UPDATES: 2 failures to 0 successes'"])
        self.assertEqual(resp.status_int, 200)
        self.assertFalse(self.controller.logger.exception.called)
        self.assertFalse(self.controller.logger.error.called)
        self.assertEqual(len(_requests), 2)  # sanity
        req = _requests.pop(0)
        self.assertEqual(req.path, '/device/partition/a/c/o1')
        self.assertEqual(req.content_length, 3)
        self.assertEqual(req.headers, {
            'Content-Length': '3',
            'X-Timestamp': '1364456113.00001',
            'Host': 'localhost:80',
            'X-Backend-Storage-Policy-Index': '0',
            'X-Backend-Replication': 'True',
            'X-Backend-Replication-Headers': (
                'content-length x-timestamp')})
        self.assertEqual(req.read_body, '12')
        req = _requests.pop(0)
        self.assertEqual(req.path, '/device/partition/a/c/o2')
        self.assertEqual(req.content_length, 1)
        self.assertEqual(req.headers, {
            'Content-Length': '1',
            'X-Timestamp': '1364456113.00002',
            'Host': 'localhost:80',
            'X-Backend-Storage-Policy-Index': '0',
            'X-Backend-Replication': 'True',
            'X-Backend-Replication-Headers': (
                'content-length x-timestamp')})
        self.assertEqual(req.read_body, '1')
        self.assertEqual(_requests, [])


@patch_policies(with_ec_default=True)
class TestSsyncRxServer(unittest.TestCase):
    # Tests to verify behavior of SSYNC requests sent to an object
    # server socket.

    def setUp(self):
        self.rx_ip = '127.0.0.1'
        # dirs
        self.tmpdir = tempfile.mkdtemp()
        self.tempdir = os.path.join(self.tmpdir, 'tmp_test_obj_server')

        self.devices = os.path.join(self.tempdir, 'srv/node')
        for device in ('sda1', 'sdb1'):
            os.makedirs(os.path.join(self.devices, device))

        self.conf = {
            'devices': self.devices,
            'swift_dir': self.tempdir,
        }
        self.rx_logger = debug_logger('test-object-server')
        rx_server = server.ObjectController(self.conf, logger=self.rx_logger)
        sock = eventlet.listen((self.rx_ip, 0))
        self.rx_server = eventlet.spawn(
            eventlet.wsgi.server, sock, rx_server, utils.NullLogger())
        self.rx_port = sock.getsockname()[1]
        self.tx_logger = debug_logger('test-reconstructor')
        self.daemon = ObjectReconstructor(self.conf, self.tx_logger)
        self.daemon._diskfile_mgr = self.daemon._df_router[POLICIES[0]]

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_SSYNC_disconnect(self):
        node = {
            'replication_ip': '127.0.0.1',
            'replication_port': self.rx_port,
            'device': 'sdb1',
        }
        job = {
            'partition': 0,
            'policy': POLICIES[0],
            'device': 'sdb1',
        }
        sender = ssync_sender.Sender(self.daemon, node, job, ['abc'])

        # kick off the sender and let the error trigger failure
        with mock.patch('swift.obj.ssync_receiver.Receiver.initialize_request')\
                as mock_initialize_request:
            mock_initialize_request.side_effect = \
                swob.HTTPInternalServerError()
            success, _ = sender()
        self.assertFalse(success)
        stderr = six.StringIO()
        with mock.patch('sys.stderr', stderr):
            # let gc and eventlet spin a bit
            del sender
            for i in range(3):
                eventlet.sleep(0)
        self.assertNotIn('ValueError: invalid literal for int() with base 16',
                         stderr.getvalue())

    def test_SSYNC_device_not_available(self):
        with mock.patch('swift.obj.ssync_receiver.Receiver.missing_check')\
                as mock_missing_check:
            self.connection = bufferedhttp.BufferedHTTPConnection(
                '127.0.0.1:%s' % self.rx_port)
            self.connection.putrequest('SSYNC', '/sdc1/0')
            self.connection.putheader('Transfer-Encoding', 'chunked')
            self.connection.putheader('X-Backend-Storage-Policy-Index',
                                      int(POLICIES[0]))
            self.connection.endheaders()
            resp = self.connection.getresponse()
        self.assertEqual(507, resp.status)
        resp.read()
        resp.close()
        # sanity check that the receiver did not proceed to missing_check
        self.assertFalse(mock_missing_check.called)

    def test_SSYNC_invalid_policy(self):
        valid_indices = sorted([int(policy) for policy in POLICIES])
        bad_index = valid_indices[-1] + 1
        with mock.patch('swift.obj.ssync_receiver.Receiver.missing_check')\
                as mock_missing_check:
            self.connection = bufferedhttp.BufferedHTTPConnection(
                '127.0.0.1:%s' % self.rx_port)
            self.connection.putrequest('SSYNC', '/sda1/0')
            self.connection.putheader('Transfer-Encoding', 'chunked')
            self.connection.putheader('X-Backend-Storage-Policy-Index',
                                      bad_index)
            self.connection.endheaders()
            resp = self.connection.getresponse()
        self.assertEqual(503, resp.status)
        resp.read()
        resp.close()
        # sanity check that the receiver did not proceed to missing_check
        self.assertFalse(mock_missing_check.called)


if __name__ == '__main__':
    unittest.main()
