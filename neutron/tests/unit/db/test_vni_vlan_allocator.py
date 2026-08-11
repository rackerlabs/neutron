# Copyright 2026 Red Hat, LLC
# All Rights Reserved.
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

import dataclasses

from neutron_lib import context
from neutron_lib.db import api as db_api

from neutron.db.models import vxlan_vlan_allocations as alloc_models
from neutron.db import vni_vlan_allocator
from neutron.services.evpn import exceptions as evpn_exc
from neutron.tests.unit import testlib_api

load_tests = testlib_api.module_load_tests

_VNI_PHYSNET = 'test-vni-physnet'
_VLAN_PHYSNET = 'test-vlan-physnet'
_OTHER_PHYSNET = 'other-physnet'
_MIN_VNI = 1
_MAX_VNI = 100
_MIN_VLAN = 1
_MAX_VLAN = 50
_VNI_RANGE = vni_vlan_allocator.ScopedRange(
    min_val=_MIN_VNI, max_val=_MAX_VNI, physnet=_VNI_PHYSNET)
_VLAN_RANGE = vni_vlan_allocator.ScopedRange(
    min_val=_MIN_VLAN, max_val=_MAX_VLAN, physnet=_VLAN_PHYSNET)


class TestVNIVLANAllocator(testlib_api.SqlTestCase):

    def setUp(self):
        super().setUp()
        self.ctx = context.Context(
            user_id=None, project_id=None, is_admin=True, overwrite=False)
        self.allocator = vni_vlan_allocator.VNIVLANAllocator(
            vni_exhausted_exc=evpn_exc.EVPNNoVniAvailable,
            vlan_exhausted_exc=evpn_exc.EVPNNoVlanAvailable,
            vni_in_use_exc=evpn_exc.EVPNVNIInUse,
        )

    def _allocate(self, vni_range=_VNI_RANGE, vlan_range=_VLAN_RANGE):
        with db_api.CONTEXT_WRITER.using(self.ctx):
            return self.allocator.allocate(self.ctx, vni_range, vlan_range)

    def _allocate_specific(self, vni, vni_physnet=_VNI_PHYSNET,
                           vlan_range=_VLAN_RANGE):
        with db_api.CONTEXT_WRITER.using(self.ctx):
            return self.allocator.allocate_specific_vni(
                self.ctx, vni, vni_physnet, vlan_range)

    def _deallocate(self, mapping_id):
        with db_api.CONTEXT_WRITER.using(self.ctx):
            self.allocator.deallocate(self.ctx, mapping_id)

    def _get_mapping(self, mapping_id):
        with db_api.CONTEXT_READER.using(self.ctx):
            return self.ctx.session.query(
                alloc_models.VNIVLANMapping
            ).filter_by(id=mapping_id).one_or_none()

    def _count_vni_allocations(self, physnet=_VNI_PHYSNET):
        with db_api.CONTEXT_READER.using(self.ctx):
            return self.ctx.session.query(
                alloc_models.VNIAllocation
            ).filter_by(physnet=physnet).count()

    def _count_vlan_allocations(self, physnet=_VLAN_PHYSNET):
        with db_api.CONTEXT_READER.using(self.ctx):
            return self.ctx.session.query(
                alloc_models.VLANAllocation
            ).filter_by(physnet=physnet).count()

    def test_allocate_returns_valid_tuple(self):
        mapping_id, vni, vlan_id = self._allocate()
        self.assertIsNotNone(mapping_id)
        self.assertGreaterEqual(vni, _MIN_VNI)
        self.assertLessEqual(vni, _MAX_VNI)
        self.assertGreaterEqual(vlan_id, _MIN_VLAN)
        self.assertLessEqual(vlan_id, _MAX_VLAN)

    def test_allocate_creates_mapping_row(self):
        mapping_id, vni, vlan_id = self._allocate()
        mapping = self._get_mapping(mapping_id)
        self.assertIsNotNone(mapping)
        self.assertEqual(vni, mapping.vni_allocation.vni)
        self.assertEqual(vlan_id, mapping.vlan_allocation.vlan_id)

    def test_allocate_sequential_distinct(self):
        _, vni1, vlan1 = self._allocate()
        _, vni2, vlan2 = self._allocate()
        self.assertNotEqual(vni1, vni2)
        self.assertNotEqual(vlan1, vlan2)

    def test_allocate_specific_vni_uses_requested_value(self):
        mapping_id, vni, vlan_id = self._allocate_specific(42)
        self.assertEqual(42, vni)
        self.assertIsNotNone(mapping_id)
        self.assertGreaterEqual(vlan_id, _MIN_VLAN)

    def test_allocate_specific_vni_duplicate_raises(self):
        self._allocate_specific(42)
        self.assertRaises(
            evpn_exc.EVPNVNIInUse, self._allocate_specific, 42)

    def test_allocate_vni_exhausted_raises(self):
        vni_range = dataclasses.replace(_VNI_RANGE, min_val=1, max_val=1)
        self._allocate(vni_range=vni_range)
        self.assertRaises(
            evpn_exc.EVPNNoVniAvailable, self._allocate, vni_range=vni_range)

    def test_allocate_vlan_exhausted_raises(self):
        vlan_range = dataclasses.replace(_VLAN_RANGE, min_val=1, max_val=1)
        self._allocate(vlan_range=vlan_range)
        self.assertRaises(
            evpn_exc.EVPNNoVlanAvailable, self._allocate,
            vlan_range=vlan_range)

    def test_deallocate_removes_all_rows(self):
        mapping_id, _, _ = self._allocate()
        self._deallocate(mapping_id)

        self.assertIsNone(self._get_mapping(mapping_id))
        self.assertEqual(0, self._count_vni_allocations())
        self.assertEqual(0, self._count_vlan_allocations())

    def test_deallocate_nonexistent_is_safe(self):
        self._deallocate(99999)

    def test_allocate_vni_scoped_by_vni_physnet(self):
        single = dataclasses.replace(_VNI_RANGE, min_val=1, max_val=1)
        mapping_a, vni_a, _ = self._allocate(vni_range=single)
        mapping_b, vni_b, _ = self._allocate(
            vni_range=dataclasses.replace(single, physnet=_OTHER_PHYSNET))
        self.assertEqual(vni_a, vni_b)
        self.assertNotEqual(mapping_a, mapping_b)

    def test_deallocate_then_reallocate_reuses_slot(self):
        vni_range = dataclasses.replace(_VNI_RANGE, min_val=1, max_val=1)
        vlan_range = dataclasses.replace(_VLAN_RANGE, min_val=1, max_val=1)
        mapping_id, vni, vlan = self._allocate(vni_range=vni_range,
                                               vlan_range=vlan_range)
        self._deallocate(mapping_id)
        _mapping_id2, vni2, vlan2 = self._allocate(vni_range=vni_range,
                                                   vlan_range=vlan_range)
        self.assertEqual(vni, vni2)
        self.assertEqual(vlan, vlan2)

    def test_allocate_writes_each_physnet_to_its_own_table(self):
        mapping_id, _, _ = self._allocate()
        mapping = self._get_mapping(mapping_id)
        self.assertEqual(_VNI_PHYSNET, mapping.vni_allocation.physnet)
        self.assertEqual(_VLAN_PHYSNET, mapping.vlan_allocation.physnet)

    def test_vlan_pools_independent_per_vlan_physnet(self):
        # A single VLAN ID may be reused for several VNIs as long as the
        # VLAN physnets differ, even though the VNIs share a pool.
        vni_range = dataclasses.replace(_VNI_RANGE, min_val=1, max_val=2)
        vlan_range = dataclasses.replace(_VLAN_RANGE, min_val=1, max_val=1)
        _, vni_a, vlan_a = self._allocate(vni_range=vni_range,
                                          vlan_range=vlan_range)
        _, vni_b, vlan_b = self._allocate(
            vni_range=vni_range,
            vlan_range=dataclasses.replace(vlan_range,
                                           physnet=_OTHER_PHYSNET))
        self.assertNotEqual(vni_a, vni_b)
        self.assertEqual(1, vlan_a)
        self.assertEqual(1, vlan_b)

    def test_vlan_pool_shared_across_vni_physnets_exhausts(self):
        # The converse: distinct VNI physnets do not give the VLAN a fresh
        # pool, because the VLAN is scoped by its own physnet.
        vlan_range = dataclasses.replace(_VLAN_RANGE, min_val=1, max_val=1)
        self._allocate(vlan_range=vlan_range)
        self.assertRaises(
            evpn_exc.EVPNNoVlanAvailable, self._allocate,
            vni_range=dataclasses.replace(_VNI_RANGE,
                                          physnet=_OTHER_PHYSNET),
            vlan_range=vlan_range)

    def test_allocate_specific_vni_scoped_by_vni_physnet(self):
        _, vni_a, _ = self._allocate_specific(42, vni_physnet=_VNI_PHYSNET)
        _, vni_b, _ = self._allocate_specific(42, vni_physnet=_OTHER_PHYSNET)
        self.assertEqual(42, vni_a)
        self.assertEqual(42, vni_b)
