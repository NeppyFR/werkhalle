"""Catalogue rules the plant, the planner and the auto-supply all rely on."""
from __future__ import annotations

import math

import pytest

from werkhalle.domain import catalog as cat
from werkhalle.domain.orders import Order, split_order


def test_catalog_reports_no_problems():
    assert cat.is_catalog_consistent() == []


def test_consistency_check_can_fail(monkeypatch):
    monkeypatch.setattr(cat, "PRESS_SCRAP_RATE", 0.05)
    assert any("SCRAP_MARGIN" in problem for problem in cat.is_catalog_consistent())


@pytest.mark.parametrize("product", cat.PRODUCTS.values(), ids=lambda p: p.id)
def test_every_single_press_is_slower_than_casting(product):
    """Otherwise one press alone could outrun the casting line and starve on billets by design."""
    cast_billets_per_min = cat.BILLETS_PER_MELT / cat.planned_minutes(cat.CAST, cat.MELT_MIN)
    for press, minutes_per_billet in product.press_min_per_billet.items():
        assert 1.0 / minutes_per_billet < cast_billets_per_min, (product.id, press)


def test_total_scrap_stays_below_the_margin():
    good_fraction = (1 - cat.PRESS_SCRAP_RATE) * (1 - cat.SAW_SCRAP_RATE)
    assert 1 - good_fraction < cat.SCRAP_MARGIN
    assert good_fraction * (1 + cat.SCRAP_MARGIN) >= 1


@pytest.mark.parametrize("product", cat.PRODUCTS.values(), ids=lambda p: p.id)
def test_min_lead_min_is_sane(product):
    lead = cat.min_lead_min(product)
    recipe = cat.RECIPES[product.recipe]
    assert lead == int(lead)
    # At least soak plus the three transports between the four ops.
    assert lead > recipe.soak_min + 3 * cat.TRANSPORT_MIN
    # Well below a working week: a due date this close must stay reachable.
    assert lead < 5 * 24 * 60
    assert cat.min_lead_min(product, 1) < lead
    # Never optimistic: planned minutes are at least the ideal minutes on the fastest press.
    fastest = min(product.press_min_per_billet.values()) * cat.MAX_BILLETS_PER_LOT
    assert lead >= fastest + recipe.soak_min


def test_min_lead_min_matches_the_published_example():
    assert cat.min_lead_min(cat.PRODUCTS["FR-60"]) == 428


def test_planned_minutes_rounds_up_and_includes_allowance():
    assert cat.planned_minutes(cat.PRESS, 56.0) == 61
    assert cat.planned_minutes(cat.AGE, 300) == 300
    assert cat.planned_minutes(cat.CAST, cat.MELT_MIN) == 93
    assert cat.planned_minutes(cat.AGE, 0) == 0


@pytest.mark.parametrize("product", cat.PRODUCTS.values(), ids=lambda p: p.id)
def test_full_lot_fits_every_furnace_and_baskets_hold_whole_strands(product):
    assert product.bars_per_basket % product.bars_per_billet == 0
    assert product.strands_per_basket >= 1
    baskets = math.ceil(cat.MAX_BILLETS_PER_LOT / product.strands_per_basket)
    for furnace in cat.MACHINES_BY_KIND[cat.AGE]:
        assert baskets <= cat.MACHINES[furnace].capacity


def test_exclusive_products_only_on_press_two():
    assert set(cat.PRODUCTS["ST-200"].press_min_per_billet) == {"PR2"}
    assert set(cat.PRODUCTS["CB-90"].press_min_per_billet) == {"PR2"}


@pytest.mark.parametrize("machine_id", cat.MACHINES_BY_KIND[cat.PRESS])
def test_press_is_never_cold_enough_to_break_the_extrusion_floor(machine_id):
    spec = cat.MACHINES[machine_id]
    assert min(spec.temp_idle_c, spec.temp_setup_c, spec.temp_run_c, spec.temp_fault_c) > 400.0


def test_casting_line_is_never_cold_enough_to_break_the_melt_floor():
    spec = cat.MACHINES["GUSS"]
    assert min(spec.temp_idle_c, spec.temp_setup_c, spec.temp_run_c, spec.temp_fault_c) > 650.0


def test_initial_stock_is_whole_melts_of_known_alloys():
    for alloy, count in cat.INITIAL_BILLET_STOCK.items():
        assert alloy in cat.ALLOYS
        assert count % cat.BILLETS_PER_MELT == 0


@pytest.mark.parametrize("machine_id", cat.MACHINE_ORDER)
def test_machine_has_faults_warning_and_positive_rates(machine_id):
    spec = cat.MACHINES[machine_id]
    assert spec.faults
    assert all(fault.repair_mean_min > 0 for fault in spec.faults)
    assert spec.mtbf_operating_min > 0
    assert spec.warning.mtbf_min > 0
    assert spec.warning.duration_min > 0
    assert spec.warning.delta_k > 0
    assert spec.temp_tau_min > 0
    assert cat.fault_spec(machine_id).code == spec.faults[0].code
    assert cat.fault_spec(machine_id, spec.faults[-1].code) == spec.faults[-1]
    with pytest.raises(KeyError):
        cat.fault_spec(machine_id, "X-999")


def test_initial_setup_classes_are_reachable_classes():
    product_classes = {
        kind: {cat.setup_class(kind, p) for p in cat.PRODUCTS.values()} for kind in cat.KINDS
    }
    for machine_id, initial in cat.INITIAL_SETUP_CLASS.items():
        assert initial in product_classes[cat.MACHINES[machine_id].kind], machine_id


def test_setup_minutes_are_sequence_dependent():
    assert cat.setup_minutes(cat.PRESS, "6060|M-1001", "6060|M-1001") == 0
    assert cat.setup_minutes(cat.PRESS, "6060|M-1001", "6060|M-1002") == 35
    assert cat.setup_minutes(cat.PRESS, "6060|M-1001", "6082|M-3001") == 55
    assert cat.setup_minutes(cat.AGE, "T66", "T5") == 30
    assert cat.setup_minutes(cat.AGE, None, "T5") == 0


@pytest.mark.parametrize("product_id", cat.PRODUCTS)
def test_split_order_leaves_scrap_margin_and_respects_lot_size(product_id):
    product = cat.PRODUCTS[product_id]
    order = Order("A-1", product_id, 300, 5000.0)
    lots = split_order(order, 7)
    assert lots[0].id == "L-00007"
    assert all(1 <= lot.n_billets <= cat.MAX_BILLETS_PER_LOT for lot in lots)
    bars = sum(lot.planned_bars for lot in lots)
    assert bars >= 300 * (1 + cat.SCRAP_MARGIN) - 1e-9
    assert bars - 300 * (1 + cat.SCRAP_MARGIN) < product.bars_per_billet
