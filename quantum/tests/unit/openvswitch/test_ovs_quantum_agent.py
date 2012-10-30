# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2012 OpenStack Foundation.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import sys

import mock
from oslo.config import cfg
import testtools

from quantum.agent.linux import ip_lib
from quantum.agent.linux import ovs_lib
from quantum.plugins.openvswitch.agent import ovs_quantum_agent
from quantum.tests import base


NOTIFIER = ('quantum.plugins.openvswitch.'
            'ovs_quantum_plugin.AgentNotifierApi')


class CreateAgentConfigMap(base.BaseTestCase):

    def test_create_agent_config_map_succeeds(self):
        self.assertTrue(ovs_quantum_agent.create_agent_config_map(cfg.CONF))

    def test_create_agent_config_map_fails_for_invalid_tunnel_config(self):
        self.addCleanup(cfg.CONF.reset)
        # An ip address is required for tunneling but there is no default
        cfg.CONF.set_override('enable_tunneling', True, group='OVS')
        with testtools.ExpectedException(ValueError):
            ovs_quantum_agent.create_agent_config_map(cfg.CONF)


class TestOvsQuantumAgent(base.BaseTestCase):

    def setUp(self):
        super(TestOvsQuantumAgent, self).setUp()
        self.addCleanup(cfg.CONF.reset)
        self.addCleanup(mock.patch.stopall)
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        # Avoid rpc initialization for unit tests
        cfg.CONF.set_override('rpc_backend',
                              'quantum.openstack.common.rpc.impl_fake')
        cfg.CONF.set_override('report_interval', 0, 'AGENT')
        kwargs = ovs_quantum_agent.create_agent_config_map(cfg.CONF)

        with contextlib.nested(
            mock.patch('quantum.plugins.openvswitch.agent.ovs_quantum_agent.'
                       'OVSQuantumAgent.setup_integration_br',
                       return_value=mock.Mock()),
            mock.patch('quantum.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01')):
            self.agent = ovs_quantum_agent.OVSQuantumAgent(**kwargs)
            self.agent.tun_br = mock.Mock()
        self.agent.sg_agent = mock.Mock()

    def get_mock_devices(self, bridge=None):
        if not bridge:
            bridge = mock.Mock()
        return [(bridge, {})]

    def _mock_port_bound(self, ofport=None):
        port = mock.Mock()
        port.ofport = ofport
        self.agent.port_bound(port, 'net-uuid', 'local', None, None)
        expected_calls = [
            mock.call.set_db_attribute('Port', port.port_name, 'tag', '1')]
        if ofport != -1:
            expected_calls.append(mock.call.delete_flows(in_port=ofport))
        port.switch.assert_has_calls(expected_calls)

    def test_port_bound_deletes_flows_for_valid_ofport(self):
        self._mock_port_bound(ofport=1)

    def test_port_bound_ignores_flows_for_invalid_ofport(self):
        self._mock_port_bound(ofport=-1)

    def test_port_dead(self):
        port = mock.Mock()
        self.agent.port_dead(port)
        port.switch.assert_has_calls([
            mock.call.set_db_attribute('Port', port.port_name, 'tag',
                                       mock.ANY),
            mock.call.add_flow(priority=2, in_port=port.ofport,
                               actions='drop'),
        ])

    def mock_update_ports(self, vif_port_set=None, registered_ports=None):
        with mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                               return_value=vif_port_set):
            return self.agent.update_ports(registered_ports)

    def test_update_ports_returns_none_for_unchanged_ports(self):
        self.assertIsNone(self.mock_update_ports(set(), set()))

    def test_update_ports_returns_port_changes(self):
        bridge = self.agent.int_br
        vif_port_set = set([1, 3])
        registered_ports = set((bridge, x) for x in [1, 2])
        expected = dict(current=set((bridge, x) for x in vif_port_set),
                        added=set([(bridge, 3)]),
                        removed=set([(bridge, 2)]))
        actual = self.mock_update_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def test_treat_devices_added_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                               side_effect=Exception()):
            devices = self.get_mock_devices()
            self.assertTrue(self.agent.treat_devices_added(devices))

    def _mock_treat_devices_added(self, details, port, func_name):
        """Mock treat devices added.

        :param details: the details to return for the device
        :param port: the port that get_vif_port_by_id should return
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """
        bridge = mock.Mock()
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              return_value=details),
            mock.patch.object(bridge, 'get_vif_port_by_id',
                              return_value=port),
            mock.patch.object(self.agent, func_name)
        ) as (get_dev_fn, get_vif_func, func):
            devices = self.get_mock_devices(bridge=bridge)
            self.assertFalse(self.agent.treat_devices_added(devices))
        return func.called

    def test_treat_devices_added_ignores_invalid_ofport(self):
        port = mock.Mock()
        port.ofport = -1
        self.assertFalse(self._mock_treat_devices_added(mock.MagicMock(), port,
                                                        'port_dead'))

    def test_treat_devices_added_marks_unknown_port_as_dead(self):
        port = mock.Mock()
        port.ofport = 1
        self.assertTrue(self._mock_treat_devices_added(mock.MagicMock(), port,
                                                       'port_dead'))

    def test_treat_devices_added_updates_known_port(self):
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        self.assertTrue(self._mock_treat_devices_added(details,
                                                       mock.Mock(),
                                                       'treat_vif_port'))

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            devices = self.get_mock_devices()
            self.assertTrue(self.agent.treat_devices_removed(devices))

    def _mock_treat_devices_removed(self, port_exists):
        details = dict(exists=port_exists)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent, 'port_unbound') as port_unbound:
                devices = self.get_mock_devices()
                self.assertFalse(self.agent.treat_devices_removed(devices))
        self.assertEqual(port_unbound.called, not port_exists)

    def test_treat_devices_removed_unbinds_port(self):
        self._mock_treat_devices_removed(True)

    def test_treat_devices_removed_ignores_missing_port(self):
        self._mock_treat_devices_removed(False)

    def test_process_network_ports(self):
        reply = {'current': set(['tap0']),
                 'removed': set(['eth0']),
                 'added': set(['eth1'])}
        with mock.patch.object(self.agent, 'treat_devices_added',
                               return_value=False) as device_added:
            with mock.patch.object(self.agent, 'treat_devices_removed',
                                   return_value=False) as device_removed:
                self.assertFalse(self.agent.process_network_ports(reply))
                self.assertTrue(device_added.called)
                self.assertTrue(device_removed.called)

    def test_report_state(self):
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, "get_vif_port_set"),
            mock.patch.object(self.agent.state_rpc, "report_state")
        ) as (get_vif_fn, report_st):
            get_vif_fn.return_value = ["vif123", "vif234"]
            self.agent._report_state()
            self.assertTrue(get_vif_fn.called)
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state)
            self.assertNotIn("start_flag", self.agent.agent_state)
            self.assertEqual(
                self.agent.agent_state["configurations"]["devices"], 2
            )

    def test_network_delete(self):
        with mock.patch.object(self.agent, "reclaim_local_vlan") as recl_fn:
            self.agent.network_delete("unused_context",
                                      network_id="123")
            self.assertFalse(recl_fn.called)

            self.agent.local_vlan_map["123"] = "LVM object"
            self.agent.network_delete("unused_context",
                                      network_id="123")
            recl_fn.assert_called_with("123", self.agent.local_vlan_map["123"])

    def test_port_update(self):
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, "get_vif_port_by_id"),
            mock.patch.object(self.agent, "treat_vif_port"),
            mock.patch.object(self.agent.plugin_rpc, "update_device_up"),
            mock.patch.object(self.agent.plugin_rpc, "update_device_down")
        ) as (getvif_fn, treatvif_fn, updup_fn, upddown_fn):
            port = {"id": "123",
                    "network_id": "124",
                    "admin_state_up": False}
            getvif_fn.return_value = "vif_port_obj"
            self.agent.port_update("unused_context",
                                   port=port,
                                   network_type="vlan",
                                   segmentation_id="1",
                                   physical_network="physnet")
            treatvif_fn.assert_called_with("vif_port_obj", "123",
                                           "124", "vlan", "physnet",
                                           "1", False)
            upddown_fn.assert_called_with(self.agent.context,
                                          "123", self.agent.agent_id)

            port["admin_state_up"] = True
            self.agent.port_update("unused_context",
                                   port=port,
                                   network_type="vlan",
                                   segmentation_id="1",
                                   physical_network="physnet")
            updup_fn.assert_called_with(self.agent.context,
                                        "123", self.agent.agent_id)

    def test_setup_physical_bridges(self):
        with contextlib.nested(
            mock.patch.object(ip_lib, "device_exists"),
            mock.patch.object(sys, "exit"),
            mock.patch.object(ovs_lib.OVSBridge, "remove_all_flows"),
            mock.patch.object(ovs_lib.OVSBridge, "add_flow"),
            mock.patch.object(ovs_lib.OVSBridge, "add_port"),
            mock.patch.object(ovs_lib.OVSBridge, "delete_port"),
            mock.patch.object(self.agent.int_br, "add_port"),
            mock.patch.object(self.agent.int_br, "delete_port"),
            mock.patch.object(ip_lib.IPWrapper, "add_veth"),
            mock.patch.object(ip_lib.IpLinkCommand, "delete"),
            mock.patch.object(ip_lib.IpLinkCommand, "set_up")
        ) as (devex_fn, sysexit_fn, remflows_fn, ovs_addfl_fn,
              ovs_addport_fn, ovs_delport_fn, br_addport_fn,
              br_delport_fn, addveth_fn, linkdel_fn, linkset_fn):
            devex_fn.return_value = True
            addveth_fn.return_value = (ip_lib.IPDevice("int-br-eth1"),
                                       ip_lib.IPDevice("phy-br-eth1"))
            ovs_addport_fn.return_value = "int_ofport"
            br_addport_fn.return_value = "phys_veth"
            self.agent.setup_physical_bridges({"physnet1": "br-eth"})
            self.assertEqual(self.agent.int_ofports["physnet1"],
                             "phys_veth")
            self.assertEqual(self.agent.phys_ofports["physnet1"],
                             "int_ofport")

    def test_port_unbound(self):
        with contextlib.nested(
            mock.patch.object(self.agent.tun_br, "delete_flows"),
            mock.patch.object(self.agent, "reclaim_local_vlan")
        ) as (delfl_fn, reclvl_fn):
            self.agent.enable_tunneling = True
            lvm = mock.Mock()
            lvm.network_type = "gre"
            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.local_vlan_map["netuid12345"] = lvm
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertTrue(delfl_fn.called)
            self.assertTrue(reclvl_fn.called)
            reclvl_fn.called = False

            lvm.vif_ports = {}
            self.agent.port_unbound("vif1", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.port_unbound("vif3", "netuid12345")
            self.assertEqual(reclvl_fn.call_count, 2)

    def test_setup_domu_integration_br_returns_none_for_missing_name(self):
        self.assertIsNone(self.agent.setup_domu_integration_br(None, None))

    def test_setup_domu_integration_br_succeeds(self):
        with mock.patch.object(self.agent, 'setup_bridge'):
            self.assertTrue(self.agent.setup_domu_integration_br('foo', None))

    def test_get_domu_mac_addresses_returns_none_for_missing_domu_bridge(self):
        self.assertIsNone(self.agent.get_domu_mac_addresses())

    def test_domu_mac_addresses_returns_addresses_for_domu_bridge(self):
        port_macs = ['ff:fe:00:00:00:00', 'ff:ff:00:00:00:00']
        expected = set(port_macs)
        self.agent.domu_int_br = mock.Mock()
        with mock.patch.object(self.agent.domu_int_br, 'get_port_macs',
                               return_value=port_macs):
            actual = self.agent.get_domu_mac_addresses()
        self.assertEqual(actual, expected)

    def test_domu_mac_addresses_returns_cached_result(self):
        self.test_domu_mac_addresses_returns_addresses_for_domu_bridge()
        # This call would fail due to lack of mocking if the result
        # was not cached.
        self.assertTrue(self.agent.get_domu_mac_addresses())

    def test_get_all_ports_set_succeeds_for_int_br(self):
        ports = set([1, 2])
        expected = set((self.agent.int_br, x) for x in ports)
        with mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                               return_value=ports):
            actual = self.agent.get_all_ports_set()
        self.assertEqual(actual, expected)

    def test_get_all_ports_succeeds_for_domu_int_br(self):
        ports = set([1, 2])
        self.agent.domu_int_br = mock.Mock()
        bridges = [self.agent.int_br, self.agent.domu_int_br]
        expected = set((bridge, port) for bridge in bridges for port in ports)
        with mock.patch.object(self.agent, 'get_domu_mac_addresses',
                               return_value=None):
            with mock.patch.object(self.agent.int_br, 'get_vif_port_set',
                                   return_value=ports):
                with mock.patch.object(self.agent.domu_int_br,
                                       'get_vif_port_set',
                                       return_value=ports):
                    actual = self.agent.get_all_ports_set()
        self.assertEqual(actual, expected)
