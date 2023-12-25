# Copyright (c) 2021-2022 VMware, Inc. All Rights Reserved.
#
# Authors: Andrew Kutz <akutz@vmware.com>
#          Pengpeng Sun <pengpengs@vmware.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import base64
import gzip
import os
from contextlib import ExitStack
from textwrap import dedent

import pytest

from cloudinit import dmi, helpers, safeyaml, settings, util
from cloudinit.sources import DataSourceVirtualbox
from cloudinit.sources.helpers.vmware.imc import guestcust_util
from cloudinit.subp import ProcessExecutionError
from tests.unittests.helpers import (
    CiTestCase,
    FilesystemMockingTestCase,
    mock,
    populate_dir,
    wrap_and_call,
)

#  DataSourceVirtualbox
MPATH = "cloudinit.sources.DataSourceVirtualbox."
PRODUCT_NAME_FILE_PATH = "/sys/class/dmi/id/product_name"
PRODUCT_NAME = "Virtualbox"
PRODUCT_UUID = "82343CED-E4C7-423B-8F6B-0D34D19067AB"
REROOT_FILES = {
    DataSourceVirtualbox.PRODUCT_UUID_FILE_PATH: PRODUCT_UUID,
    PRODUCT_NAME_FILE_PATH: PRODUCT_NAME,
}

VMW_MULTIPLE_KEYS = [
    "ssh-rsa AAAAB3NzaC1yc2EAAAA... test1@vmw.com",
    "ssh-rsa AAAAB3NzaC1yc2EAAAA... test2@vmw.com",
]
VMW_SINGLE_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAA... test@vmw.com"

VMW_METADATA_YAML = """instance-id: cloud-vm
local-hostname: cloud-vm
network:
  version: 2
  ethernets:
    nics:
      match:
        name: ens*
      dhcp4: yes
"""

VMW_USERDATA_YAML = """## template: jinja
#cloud-config
users:
- default
"""

VMW_VENDORDATA_YAML = """## template: jinja
#cloud-config
runcmd:
- echo "Hello, world."
"""

@pytest.fixture(autouse=True)
def common_patches():
    mocks = [
        mock.patch("cloudinit.util.platform.platform", return_value="Linux"),
        mock.patch.multiple(
            "cloudinit.dmi",
            is_container=mock.Mock(return_value=False),
            is_FreeBSD=mock.Mock(return_value=False),
        ),
        mock.patch(
            "cloudinit.sources.DataSourceVirtualbox.netifaces.interfaces",
            return_value=[],
        ),
        mock.patch(
            "cloudinit.sources.DataSourceVirtualbox.getfqdn",
            return_value="host.cloudinit.test",
        ),
    ]
    with ExitStack() as stack:
        for some_mock in mocks:
            stack.enter_context(some_mock)
        yield


class TestDataSourceVirtualbox(CiTestCase):
    """
    Test common functionality that is not transport specific.
    """

    with_logs = True

    def setUp(self):
        super(TestDataSourceVirtualbox, self).setUp()
        self.tmp = self.tmp_dir()

    def test_no_data_access_method(self):
        ds = get_ds(self.tmp)
        with mock.patch(
            "cloudinit.sources.DataSourceVirtualbox.is_Virtualbox_platform",
            return_value=False,
        ):
            ret = ds.get_data()
        self.assertFalse(ret)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.get_default_ip_addrs")
    def test_get_host_info_ipv4(self, m_fn_ipaddr):
        m_fn_ipaddr.return_value = ("10.10.10.1", None)
        host_info = DataSourceVirtualbox.get_host_info()
        self.assertTrue(host_info)
        self.assertTrue(host_info["hostname"])
        self.assertTrue(host_info["hostname"] == "host.cloudinit.test")
        self.assertTrue(host_info["local-hostname"])
        self.assertTrue(host_info["local_hostname"])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4] == "10.10.10.1")
        self.assertFalse(host_info.get(DataSourceVirtualbox.LOCAL_IPV6))

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.get_default_ip_addrs")
    def test_get_host_info_ipv6(self, m_fn_ipaddr):
        m_fn_ipaddr.return_value = (None, "2001:db8::::::8888")
        host_info = DataSourceVirtualbox.get_host_info()
        self.assertTrue(host_info)
        self.assertTrue(host_info["hostname"])
        self.assertTrue(host_info["hostname"] == "host.cloudinit.test")
        self.assertTrue(host_info["local-hostname"])
        self.assertTrue(host_info["local_hostname"])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV6])
        self.assertTrue(
            host_info[DataSourceVirtualbox.LOCAL_IPV6] == "2001:db8::::::8888"
        )
        self.assertFalse(host_info.get(DataSourceVirtualbox.LOCAL_IPV4))

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.get_default_ip_addrs")
    def test_get_host_info_dual(self, m_fn_ipaddr):
        m_fn_ipaddr.return_value = ("10.10.10.1", "2001:db8::::::8888")
        host_info = DataSourceVirtualbox.get_host_info()
        self.assertTrue(host_info)
        self.assertTrue(host_info["hostname"])
        self.assertTrue(host_info["hostname"] == "host.cloudinit.test")
        self.assertTrue(host_info["local-hostname"])
        self.assertTrue(host_info["local_hostname"])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4] == "10.10.10.1")
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV6])
        self.assertTrue(
            host_info[DataSourceVirtualbox.LOCAL_IPV6] == "2001:db8::::::8888"
        )

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.get_host_info")
    def test_wait_on_network(self, m_fn):
        metadata = {
            DataSourceVirtualbox.WAIT_ON_NETWORK: {
                DataSourceVirtualbox.WAIT_ON_NETWORK_IPV4: True,
                DataSourceVirtualbox.WAIT_ON_NETWORK_IPV6: False,
            },
        }
        m_fn.side_effect = [
            {
                "hostname": "host.cloudinit.test",
                "local-hostname": "host.cloudinit.test",
                "local_hostname": "host.cloudinit.test",
                "network": {
                    "interfaces": {
                        "by-ipv4": {},
                        "by-ipv6": {},
                        "by-mac": {
                            "aa:bb:cc:dd:ee:ff": {"ipv4": [], "ipv6": []}
                        },
                    },
                },
            },
            {
                "hostname": "host.cloudinit.test",
                "local-hostname": "host.cloudinit.test",
                "local-ipv4": "10.10.10.1",
                "local_hostname": "host.cloudinit.test",
                "network": {
                    "interfaces": {
                        "by-ipv4": {
                            "10.10.10.1": {
                                "mac": "aa:bb:cc:dd:ee:ff",
                                "netmask": "255.255.255.0",
                            }
                        },
                        "by-mac": {
                            "aa:bb:cc:dd:ee:ff": {
                                "ipv4": [
                                    {
                                        "addr": "10.10.10.1",
                                        "broadcast": "10.10.10.255",
                                        "netmask": "255.255.255.0",
                                    }
                                ],
                                "ipv6": [],
                            }
                        },
                    },
                },
            },
        ]

        host_info = DataSourceVirtualbox.wait_on_network(metadata)

        logs = self.logs.getvalue()
        expected_logs = [
            "DEBUG: waiting on network: wait4=True, "
            "ready4=False, wait6=False, ready6=False\n",
            "DEBUG: waiting on network complete\n",
        ]
        for log in expected_logs:
            self.assertIn(log, logs)

        self.assertTrue(host_info)
        self.assertTrue(host_info["hostname"])
        self.assertTrue(host_info["hostname"] == "host.cloudinit.test")
        self.assertTrue(host_info["local-hostname"])
        self.assertTrue(host_info["local_hostname"])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4])
        self.assertTrue(host_info[DataSourceVirtualbox.LOCAL_IPV4] == "10.10.10.1")


class TestDataSourceVirtualboxEnvVars(FilesystemMockingTestCase):
    """
    Test the envvar transport.
    """

    def setUp(self):
        super(TestDataSourceVirtualboxEnvVars, self).setUp()
        self.tmp = self.tmp_dir()
        os.environ[DataSourceVirtualbox.VMX_GUESTINFO] = "1"
        self.create_system_files()

    def tearDown(self):
        del os.environ[DataSourceVirtualbox.VMX_GUESTINFO]
        return super(TestDataSourceVirtualboxEnvVars, self).tearDown()

    def create_system_files(self):
        rootd = self.tmp_dir()
        populate_dir(
            rootd,
            {
                DataSourceVirtualbox.PRODUCT_UUID_FILE_PATH: PRODUCT_UUID,
            },
        )
        self.assertTrue(self.reRoot(rootd))

    def assert_get_data_ok(self, m_fn, m_fn_call_count=6):
        ds = get_ds(self.tmp)
        ret = ds.get_data()
        self.assertTrue(ret)
        self.assertEqual(m_fn_call_count, m_fn.call_count)
        self.assertEqual(
            ds.data_access_method, DataSourceVirtualbox.DATA_ACCESS_METHOD_ENVVAR
        )
        return ds

    def assert_metadata(self, metadata, m_fn, m_fn_call_count=6):
        ds = self.assert_get_data_ok(m_fn, m_fn_call_count)
        assert_metadata(self, ds, metadata)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_subplatform(self, m_fn):
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", "", "", ""]
        ds = self.assert_get_data_ok(m_fn, m_fn_call_count=4)
        self.assertEqual(
            ds.subplatform,
            "%s (%s)"
            % (
                DataSourceVirtualbox.DATA_ACCESS_METHOD_ENVVAR,
                DataSourceVirtualbox.get_guestinfo_envvar_key_name("metadata"),
            ),
        )

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_metadata_only(self, m_fn):
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", "", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_userdata_only(self, m_fn):
        m_fn.side_effect = ["", VMW_USERDATA_YAML, "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_vendordata_only(self, m_fn):
        m_fn.side_effect = ["", "", VMW_VENDORDATA_YAML, ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_metadata_base64(self, m_fn):
        data = base64.b64encode(VMW_METADATA_YAML.encode("utf-8"))
        m_fn.side_effect = [data, "base64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_metadata_b64(self, m_fn):
        data = base64.b64encode(VMW_METADATA_YAML.encode("utf-8"))
        m_fn.side_effect = [data, "b64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_metadata_gzip_base64(self, m_fn):
        data = VMW_METADATA_YAML.encode("utf-8")
        data = gzip.compress(data)
        data = base64.b64encode(data)
        m_fn.side_effect = [data, "gzip+base64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_get_data_metadata_gz_b64(self, m_fn):
        data = VMW_METADATA_YAML.encode("utf-8")
        data = gzip.compress(data)
        data = base64.b64encode(data)
        m_fn.side_effect = [data, "gz+b64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_metadata_single_ssh_key(self, m_fn):
        metadata = DataSourceVirtualbox.load_json_or_yaml(VMW_METADATA_YAML)
        metadata["public_keys"] = VMW_SINGLE_KEY
        metadata_yaml = safeyaml.dumps(metadata)
        m_fn.side_effect = [metadata_yaml, "", "", ""]
        self.assert_metadata(metadata, m_fn, m_fn_call_count=4)

    @mock.patch(
        "cloudinit.sources.DataSourceVirtualbox.guestinfo_envvar_get_value"
    )
    def test_metadata_multiple_ssh_keys(self, m_fn):
        metadata = DataSourceVirtualbox.load_json_or_yaml(VMW_METADATA_YAML)
        metadata["public_keys"] = VMW_MULTIPLE_KEYS
        metadata_yaml = safeyaml.dumps(metadata)
        m_fn.side_effect = [metadata_yaml, "", "", ""]
        self.assert_metadata(metadata, m_fn, m_fn_call_count=4)


class TestDataSourceVirtualboxGuestInfo(FilesystemMockingTestCase):
    """
    Test the guestinfo transport on a Virtualbox platform.
    """

    def setUp(self):
        super(TestDataSourceVirtualboxGuestInfo, self).setUp()
        self.tmp = self.tmp_dir()
        self.create_system_files()

    def create_system_files(self):
        rootd = self.tmp_dir()
        populate_dir(
            rootd,
            {
                DataSourceVirtualbox.PRODUCT_UUID_FILE_PATH: PRODUCT_UUID,
                PRODUCT_NAME_FILE_PATH: PRODUCT_NAME,
            },
        )
        self.assertTrue(self.reRoot(rootd))

    def assert_get_data_ok(self, m_fn, m_fn_call_count=6):
        ds = get_ds(self.tmp)
        ret = ds.get_data()
        self.assertEqual(m_fn_call_count, m_fn.call_count)
        self.assertTrue(ret)
        self.assertEqual(
            ds.data_access_method,
            DataSourceVirtualbox.DATA_ACCESS_METHOD_GUESTINFO,
        )
        return ds

    def assert_metadata(self, metadata, m_fn, m_fn_call_count=6):
        ds = self.assert_get_data_ok(m_fn, m_fn_call_count)
        assert_metadata(self, ds, metadata)

    def test_ds_valid_on_vmware_platform(self):
        system_type = dmi.read_dmi_data("system-product-name")
        self.assertEqual(system_type, PRODUCT_NAME)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_subplatform(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", "", "", ""]
        ds = self.assert_get_data_ok(m_fn, m_fn_call_count=4)
        self.assertEqual(
            ds.subplatform,
            "%s (%s)"
            % (
                DataSourceVirtualbox.DATA_ACCESS_METHOD_GUESTINFO,
                DataSourceVirtualbox.get_guestproperty_key_name("metadata"),
            ),
        )

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_with_vmware_rpctool(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.exec_vboxcontrol")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_non_zero_exit_code_fallback_to_vmtoolsd(
        self, m_which_fn, m_exec_vboxcontrol_fn, m_fn
    ):
        m_which_fn.side_effect = ["vboxcontrol"]
        m_exec_vboxcontrol_fn.side_effect = ProcessExecutionError(
            exit_code=1
        )
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.exec_vboxcontrol")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_vmware_rpctool_not_found_fallback_to_vmtoolsd(
        self, m_which_fn, m_exec_vboxcontrol_fn, m_fn
    ):
        m_which_fn.side_effect = ["vboxcontrol", None]
        m_fn.side_effect = [VMW_METADATA_YAML, "", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_userdata_only(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        m_fn.side_effect = ["", VMW_USERDATA_YAML, "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_vendordata_only(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        m_fn.side_effect = ["", "", VMW_VENDORDATA_YAML, ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_metadata_single_ssh_key(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        metadata = DataSourceVirtualbox.load_json_or_yaml(VMW_METADATA_YAML)
        metadata["public_keys"] = VMW_SINGLE_KEY
        metadata_yaml = safeyaml.dumps(metadata)
        m_fn.side_effect = [metadata_yaml, "", "", ""]
        self.assert_metadata(metadata, m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_metadata_multiple_ssh_keys(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        metadata = DataSourceVirtualbox.load_json_or_yaml(VMW_METADATA_YAML)
        metadata["public_keys"] = VMW_MULTIPLE_KEYS
        metadata_yaml = safeyaml.dumps(metadata)
        m_fn.side_effect = [metadata_yaml, "", "", ""]
        self.assert_metadata(metadata, m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_base64(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        data = base64.b64encode(VMW_METADATA_YAML.encode("utf-8"))
        m_fn.side_effect = [data, "base64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_b64(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["VBoxControl"]
        data = base64.b64encode(VMW_METADATA_YAML.encode("utf-8"))
        m_fn.side_effect = [data, "b64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_gzip_base64(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        data = VMW_METADATA_YAML.encode("utf-8")
        data = gzip.compress(data)
        data = base64.b64encode(data)
        m_fn.side_effect = [data, "gzip+base64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_metadata_gz_b64(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["vboxcontrol"]
        data = VMW_METADATA_YAML.encode("utf-8")
        data = gzip.compress(data)
        data = base64.b64encode(data)
        m_fn.side_effect = [data, "gz+b64", "", ""]
        self.assert_get_data_ok(m_fn, m_fn_call_count=4)

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    @mock.patch("cloudinit.sources.DataSourceVirtualbox.which")
    def test_get_data_userdata_list(self, m_which_fn, m_fn):
        m_which_fn.side_effect = ["VBoxControl"]
        
        metadata = base64.b64encode(VMW_METADATA_YAML.encode("utf-8"))
        first = metadata[0:len(metadata)//2]
        second = metadata[len(metadata)//2:]

        userdata = VMW_USERDATA_YAML.encode("utf-8")

        m_fn.side_effect = ["metadata1:metadata2", "list", 
                            first, "binary", 
                            second, "binary",
                            userdata,"plain", 
                            "","" # vendor-data
                            ]
        
        self.assert_get_data_ok(m_fn, m_fn_call_count=9)


class TestDataSourceVirtualboxGuestInfo_InvalidPlatform(FilesystemMockingTestCase):
    """
    Test the guestinfo transport on a non-VMware platform.
    """

    def setUp(self):
        super(TestDataSourceVirtualboxGuestInfo_InvalidPlatform, self).setUp()
        self.tmp = self.tmp_dir()
        self.create_system_files()

    def create_system_files(self):
        rootd = self.tmp_dir()
        populate_dir(
            rootd,
            {
                DataSourceVirtualbox.PRODUCT_UUID_FILE_PATH: PRODUCT_UUID,
            },
        )
        self.assertTrue(self.reRoot(rootd))

    @mock.patch("cloudinit.sources.DataSourceVirtualbox.guestinfo_get_value")
    def test_ds_invalid_on_non_vmware_platform(self, m_fn):
        system_type = dmi.read_dmi_data("system-product-name")
        self.assertEqual(system_type, None)

        m_fn.side_effect = [VMW_METADATA_YAML, "", "", "", "", ""]
        ds = get_ds(self.tmp)
        ret = ds.get_data()
        self.assertFalse(ret)
        
def assert_metadata(test_obj, ds, metadata):
    test_obj.assertEqual(metadata.get("instance-id"), ds.get_instance_id())
    test_obj.assertEqual(
        metadata.get("local-hostname"), ds.get_hostname().hostname
    )

    expected_public_keys = metadata.get("public_keys")
    if not isinstance(expected_public_keys, list):
        expected_public_keys = [expected_public_keys]

    test_obj.assertEqual(expected_public_keys, ds.get_public_ssh_keys())
    test_obj.assertIsInstance(ds.get_public_ssh_keys(), list)


def get_ds(temp_dir):
    ds = DataSourceVirtualbox.DataSourceVirtualbox(
        settings.CFG_BUILTIN, None, helpers.Paths({"run_dir": temp_dir})
    )
    return ds
