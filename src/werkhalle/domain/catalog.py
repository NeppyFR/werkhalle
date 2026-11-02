"""Static description of the plant: machines, products, recipes, setup rules, fault codes.

All times are simulated minutes. Shared by the plant, the schedulers and the twin, so plan and plant agree.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

CAST = "cast"
PRESS = "press"
AGE = "age"
SAW = "saw"
PACK = "pack"
KINDS = (CAST, PRESS, AGE, SAW, PACK)
LOT_ROUTE = (PRESS, AGE, SAW, PACK)

BILLETS_PER_MELT = 24
MELT_MIN = 90.0
TRANSPORT_MIN = 8
MAX_BILLETS_PER_LOT = 8
SCRAP_MARGIN = 0.03

# 1 piece = 1 bar: the saw trims bars to final length, it does not multiply them.
# Together these must stay below SCRAP_MARGIN, or lots would routinely under-deliver.
PRESS_SCRAP_RATE = 0.01
SAW_SCRAP_RATE = 0.008

# Billets on hand at t=0, as whole historic melts, so the hall moves at once and every billet has a melt.
INITIAL_BILLET_STOCK = {"6060": 24, "6063": 24, "6082": 24}
INITIAL_MELT_PLAN_ID = "BESTAND"

# Mean of real duration / ideal duration. Soak is recipe-controlled, so AGE has no noise at all.
PLANNING_ALLOWANCE = {CAST: 1.03, PRESS: 1.08, AGE: 1.0, SAW: 1.06, PACK: 1.05}


@dataclass(frozen=True)
class FaultSpec:
    code: str
    text: str
    repair_mean_min: float


@dataclass(frozen=True)
class WarningSpec:
    """Self-clearing temperature excursion. Raises an alarm of severity 500 and adds delta_k to Temperature."""

    code: str
    text: str
    delta_k: float
    duration_min: float
    mtbf_min: float


@dataclass(frozen=True)
class MachineSpec:
    """Physical parameters of one machine.

    Temperature follows a first-order lag toward the setpoint of the current state. Furnaces ignore
    temp_setup_c/temp_run_c: their setpoint in SETUP and RUN is the recipe temperature. In FAULT the setpoint is
    temp_fault_c, because a stopped press or casting line is kept hot rather than left to fall to ambient.
    """

    id: str
    name: str
    kind: str
    unit: str  # German label of what TotalCount counts
    capacity: int  # baskets for batch furnaces, 1 otherwise
    temp_ambient_c: float
    temp_idle_c: float
    temp_setup_c: float
    temp_run_c: float
    temp_fault_c: float
    temp_tau_min: float
    power_idle_kw: float
    power_setup_kw: float
    power_run_kw: float
    power_fault_kw: float
    mtbf_operating_min: float
    faults: tuple[FaultSpec, ...]
    warning: WarningSpec


_PRESS_FAULTS = (
    FaultSpec("P-201", "Hydraulikdruck zu niedrig", 90.0),
    FaultSpec("P-202", "Matrizenbruch", 150.0),
    FaultSpec("P-203", "Bolzenerwärmung ausgefallen", 60.0),
    FaultSpec("P-204", "Hauptzylinder undicht", 240.0),
)
_FURNACE_FAULTS = (
    FaultSpec("O-301", "Umluftgebläse defekt", 75.0),
    FaultSpec("O-302", "Brennerstörung", 60.0),
)

MACHINES: dict[str, MachineSpec] = {
    m.id: m
    for m in (
        MachineSpec(
            "GUSS", "Gießanlage", CAST, "Bolzen", 1,
            25.0, 680.0, 700.0, 720.0, 680.0, 30.0,
            320.0, 480.0, 850.0, 180.0, 2600.0,
            (FaultSpec("G-101", "Kühlwasserdruck zu niedrig", 60.0),
             FaultSpec("G-102", "Kokille undicht", 150.0)),
            WarningSpec("G-W01", "Schmelzetemperatur außerhalb des Toleranzbands", 14.0, 30.0, 1500.0),
        ),
        MachineSpec(
            "PR1", "Presse 1 (25 MN)", PRESS, "Stangen", 1,
            25.0, 430.0, 450.0, 480.0, 430.0, 18.0,
            90.0, 120.0, 325.0, 40.0, 2200.0, _PRESS_FAULTS,
            WarningSpec("P-W01", "Blocktemperatur über Sollwert", 12.0, 25.0, 1400.0),
        ),
        MachineSpec(
            "PR2", "Presse 2 (35 MN)", PRESS, "Stangen", 1,
            25.0, 430.0, 450.0, 490.0, 430.0, 20.0,
            120.0, 160.0, 450.0, 55.0, 2000.0, _PRESS_FAULTS,
            WarningSpec("P-W01", "Blocktemperatur über Sollwert", 12.0, 25.0, 1400.0),
        ),
        MachineSpec(
            "OF1", "Auslagerungsofen 1", AGE, "Körbe", 6,
            25.0, 120.0, 185.0, 185.0, 120.0, 12.0,
            25.0, 170.0, 180.0, 8.0, 3000.0, _FURNACE_FAULTS,
            WarningSpec("O-W01", "Ofentemperatur außerhalb des Toleranzbands", 7.0, 25.0, 1800.0),
        ),
        MachineSpec(
            "OF2", "Auslagerungsofen 2", AGE, "Körbe", 10,
            25.0, 120.0, 185.0, 185.0, 120.0, 12.0,
            35.0, 240.0, 260.0, 10.0, 3000.0, _FURNACE_FAULTS,
            WarningSpec("O-W01", "Ofentemperatur außerhalb des Toleranzbands", 7.0, 25.0, 1800.0),
        ),
        MachineSpec(
            "SAE", "Fertigsäge", SAW, "Stangen", 1,
            22.0, 24.0, 24.0, 38.0, 24.0, 12.0,
            6.0, 10.0, 45.0, 2.0, 2800.0,
            (FaultSpec("S-401", "Sägeblatt gebrochen", 35.0),
             FaultSpec("S-402", "Spannvorrichtung blockiert", 20.0)),
            WarningSpec("S-W01", "Sägeblatt läuft heiß", 9.0, 20.0, 2400.0),
        ),
        MachineSpec(
            "VP", "Verpackung", PACK, "Pakete", 1,
            22.0, 23.0, 23.0, 26.0, 23.0, 15.0,
            4.0, 6.0, 25.0, 1.0, 3200.0,
            (FaultSpec("V-501", "Umreifungsgerät blockiert", 18.0),
             FaultSpec("V-502", "Folienwickler gerissen", 12.0)),
            WarningSpec("V-W01", "Antrieb Rollenbahn warm", 5.0, 20.0, 3000.0),
        ),
    )
}

MACHINE_ORDER = tuple(MACHINES)
MACHINES_BY_KIND = {k: tuple(m.id for m in MACHINES.values() if m.kind == k) for k in KINDS}

INITIAL_SETUP_CLASS = {
    "GUSS": "6060",
    "PR1": "6060|M-1001",
    "PR2": "6082|M-3001",
    "OF1": "T66",
    "OF2": "T6",
    "SAE": "L6000",
    "VP": "B-16",
}


@dataclass(frozen=True)
class Recipe:
    id: str
    temp_c: float
    soak_min: int


RECIPES: dict[str, Recipe] = {
    r.id: r for r in (Recipe("T5", 200.0, 180), Recipe("T66", 185.0, 300), Recipe("T6", 175.0, 360))
}


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    alloy: str
    recipe: str
    die: str
    press_min_per_billet: dict[str, float]  # press id -> ideal minutes per billet; absent = not eligible
    bars_per_billet: int
    kg_per_bar: float
    bars_per_basket: int
    bars_per_package: int
    length_mm: int

    @property
    def strands_per_basket(self) -> int:
        """Baskets take whole strands only, so a strand never straddles two baskets."""
        return self.bars_per_basket // self.bars_per_billet


PRODUCTS: dict[str, Product] = {
    p.id: p
    for p in (
        Product("FR-60", "Fensterrahmen FR-60", "6060", "T66", "M-1001",
                {"PR1": 7.0, "PR2": 8.0}, 8, 7.2, 32, 16, 6000),
        Product("FP-120", "Fassadenpfosten FP-120", "6060", "T66", "M-1002",
                {"PR1": 9.0, "PR2": 8.4}, 5, 14.0, 20, 10, 6500),
        Product("KK-80", "Kühlkörper KK-80", "6063", "T5", "M-2001",
                {"PR1": 6.0, "PR2": 7.2}, 10, 3.8, 40, 20, 3000),
        Product("LS-40", "Lichtschiene LS-40", "6063", "T5", "M-2002",
                {"PR1": 5.6, "PR2": 6.8}, 12, 2.4, 48, 24, 4000),
        Product("ST-200", "Strukturprofil ST-200", "6082", "T6", "M-3001",
                {"PR2": 11.0}, 4, 18.0, 16, 8, 7000),
        Product("CB-90", "Crashbox-Profil CB-90", "6082", "T6", "M-3002",
                {"PR2": 10.0}, 5, 11.0, 20, 10, 5000),
    )
}

ALLOYS = tuple(sorted({p.alloy for p in PRODUCTS.values()}))

SAW_MIN_PER_BAR = 0.25
PACK_MIN_PER_PACKAGE = 6.0


def fault_spec(machine_id: str, code: str | None = None) -> FaultSpec:
    """Return the named fault code, or the machine's first code when none is given."""
    faults = MACHINES[machine_id].faults
    if not code:
        return faults[0]
    for fault in faults:
        if fault.code == code:
            return fault
    raise KeyError(f"{machine_id} has no fault code {code!r}")


def setup_class(kind: str, product: Product) -> str:
    """Class that decides whether changing between two ops needs a setup."""
    if kind == CAST:
        return product.alloy
    if kind == PRESS:
        return f"{product.alloy}|{product.die}"
    if kind == AGE:
        return product.recipe
    if kind == SAW:
        return f"L{product.length_mm}"
    if kind == PACK:
        return f"B-{product.bars_per_package}"
    raise ValueError(kind)


def setup_minutes(kind: str, from_class: str | None, to_class: str) -> int:
    """Sequence-dependent setup. `from_class=None` means nothing to change from."""
    if from_class is None or from_class == to_class:
        return 0
    if kind == CAST:
        return 45
    if kind == PRESS:
        alloy_changes = from_class.split("|")[0] != to_class.split("|")[0]
        return 55 if alloy_changes else 35
    if kind == AGE:
        return 30
    if kind == SAW:
        return 15
    if kind == PACK:
        return 10
    raise ValueError(kind)


def ideal_press_min(product: Product, machine_id: str, n_billets: int) -> float:
    return product.press_min_per_billet[machine_id] * n_billets


def planned_minutes(kind: str, ideal_min: float) -> int:
    """Whole minutes the planner reserves for an op whose ideal time is `ideal_min`."""
    return int(math.ceil(ideal_min * PLANNING_ALLOWANCE[kind] - 1e-9))


def ideal_age_min_per_basket(recipe: Recipe, machine_id: str) -> float:
    """ISO 22400 style per-item time of a batch: a partly filled load shows up as lost performance."""
    return recipe.soak_min / MACHINES[machine_id].capacity


def min_lead_min(product: Product, n_billets: int = MAX_BILLETS_PER_LOT) -> int:
    """Lower bound on order lead time in an empty plant: no wait for a melt or a furnace, no setups."""
    press = min(product.press_min_per_billet.values()) * n_billets
    bars = n_billets * product.bars_per_billet
    packages = math.ceil(bars / product.bars_per_package)
    return (
        planned_minutes(PRESS, press)
        + planned_minutes(AGE, RECIPES[product.recipe].soak_min)
        + planned_minutes(SAW, bars * SAW_MIN_PER_BAR)
        + planned_minutes(PACK, packages * PACK_MIN_PER_PACKAGE)
        + 3 * TRANSPORT_MIN
    )


def is_catalog_consistent() -> list[str]:
    """Sanity checks used by the tests. Returns human-readable problems, empty when fine."""
    problems: list[str] = []
    for product in PRODUCTS.values():
        if product.recipe not in RECIPES:
            problems.append(f"{product.id}: unknown recipe {product.recipe}")
        for press in product.press_min_per_billet:
            if press not in MACHINES_BY_KIND[PRESS]:
                problems.append(f"{product.id}: {press} is not a press")
        if not product.press_min_per_billet:
            problems.append(f"{product.id}: no eligible press")
        if product.bars_per_basket % product.bars_per_billet:
            problems.append(f"{product.id}: a basket must hold a whole number of strands")
        max_baskets = math.ceil(MAX_BILLETS_PER_LOT / product.strands_per_basket)
        for furnace in MACHINES_BY_KIND[AGE]:
            if max_baskets > MACHINES[furnace].capacity:
                problems.append(f"{product.id}: a full lot does not fit into {furnace}")
    for machine_id in MACHINES:
        if machine_id not in INITIAL_SETUP_CLASS:
            problems.append(f"{machine_id}: no initial setup class")
    if (1 - PRESS_SCRAP_RATE) * (1 - SAW_SCRAP_RATE) < 1 / (1 + SCRAP_MARGIN):
        problems.append("expected scrap exceeds SCRAP_MARGIN, lots would under-deliver")
    return problems
