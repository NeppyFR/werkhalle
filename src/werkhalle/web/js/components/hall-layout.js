// Geometry of the hall, in viewBox units. Flow: top bay left to right, down the transfer car, bottom bay right to left.
export const VIEW = { w: 1480, h: 600 };
export const PLATE = { w: 212, h: 74 };

export const BAYS = {
  top: { y: 16, h: 306 },
  aisle: { y: 322, h: 22 },
  bottom: { y: 344, h: 240 },
};

// Painted floor labels of the production areas.
export const AREAS = [
  { label: 'GIESSEREI', x: 22, y: 27 },
  { label: 'PRESSENHALLE', x: 424, y: 27 },
  { label: 'AUSLAGERUNG', x: 1014, y: 27 },
  { label: 'ADJUSTAGE', x: 652, y: 359 },
];

export const COLUMNS = [258, 410, 642, 880, 1008, 1200, 1340];

export const STATIONS = {
  GUSS: { plate: [28, 32], hit: [24, 28, 224, 280], work: [28, 110, 216, 192] },
  PR1: { cy: 124, x0: 424, plate: [662, 32], hit: [418, 30, 584, 146], work: [418, 64, 584, 110] },
  PR2: { cy: 272, x0: 424, plate: [662, 180], hit: [418, 178, 584, 142], work: [418, 212, 584, 106] },
  OF1: { y0: 34, x0: 1128, plate: [1252, 34], hit: [1120, 30, 348, 126], work: [1120, 30, 128, 126] },
  OF2: { y0: 182, x0: 1128, plate: [1252, 182], hit: [1120, 178, 348, 126], work: [1120, 178, 128, 126] },
  SAE: { plate: [744, 352], hit: [650, 348, 406, 212], work: [650, 432, 406, 128] },
  VP: { plate: [240, 352], hit: [204, 348, 292, 212], work: [204, 432, 292, 128] },
};

export const BUFFERS = {
  BILLET_STORE: { x: 262, y: 22, w: 142, h: 276 },
  AGE_IN: { x: 1012, y: 32, w: 92, h: 284, cols: 3 },
  SAW_IN: { x: 1062, y: 368, w: 132, h: 210, cols: 4 },
  PACK_IN: { x: 502, y: 368, w: 140, h: 210 },
  SHIP: { x: 22, y: 368, w: 174, h: 210 },
};

export const LEGEND = { x: 1208, y: 352, w: 256, h: 226 };

const PR1_CY = STATIONS.PR1.cy;
const PR2_CY = STATIONS.PR2.cy;

// Conveyor runs. A transport moves along the run of its (from, to) pair, proportional to arc length.
export const RUNS = {
  'GUSS>BILLET_STORE': [[196, 306], [336, 306]],
  'BILLET_STORE>PR1': [[390, 176], [412, 176], [412, PR1_CY - 49], [572, PR1_CY - 49]],
  'BILLET_STORE>PR2': [[390, 176], [412, 176], [412, PR2_CY - 49], [572, PR2_CY - 49]],
  'PR1>AGE_IN': [[884, PR1_CY + 30], [1022, PR1_CY + 30]],
  'PR2>AGE_IN': [[884, PR2_CY + 30], [1022, PR2_CY + 30]],
  'AGE_IN>OF1': [[1096, 90], [1114, 90], [1132, 90]],
  'AGE_IN>OF2': [[1096, 238], [1114, 238], [1132, 238]],
  'OF1>SAW_IN': [[1132, 100], [1114, 100], [1114, 368]],
  'OF2>SAW_IN': [[1132, 248], [1114, 248], [1114, 368]],
  'SAW_IN>SAE': [[1064, 500], [960, 500]],
  'SAE>PACK_IN': [[820, 500], [634, 500]],
  'PACK_IN>VP': [[506, 500], [420, 500]],
  'VP>SHIP': [[340, 500], [198, 500]],
};

// Where a transport from or to an unknown pair falls back to, so a new route never makes material vanish.
export const ANCHORS = {
  GUSS: [196, 306],
  BILLET_STORE: [336, 176],
  PR1: [572, PR1_CY - 49],
  PR2: [572, PR2_CY - 49],
  AGE_IN: [1022, 170],
  OF1: [1132, 90],
  OF2: [1132, 238],
  SAW_IN: [1114, 368],
  SAE: [850, 500],
  PACK_IN: [634, 500],
  VP: [340, 500],
  SHIP: [198, 500],
};

// Which machines feed which, for telling apart "idle" and "starved" when a frame carries no waiting_on.
export const UPSTREAM = {
  PR1: ['GUSS'],
  PR2: ['GUSS'],
  OF1: ['PR1', 'PR2'],
  OF2: ['PR1', 'PR2'],
  SAE: ['OF1', 'OF2'],
  VP: ['SAE'],
};

// Local pick-up moves. The plant moves material into a machine atomically at start, so the hall draws that hand-over itself.
export const PICKUP = {
  press: { from: 'BILLET_STORE', kind: 'billets' },
  age: { from: 'AGE_IN', kind: 'baskets' },
  saw: { from: 'SAW_IN', kind: 'baskets' },
  pack: { from: 'PACK_IN', kind: 'pieces' },
};

export const KIND_OF = { GUSS: 'cast', PR1: 'press', PR2: 'press', OF1: 'age', OF2: 'age', SAE: 'saw', VP: 'pack' };
export const CAPACITY = { OF1: 6, OF2: 10 };
export const ALLOY_ORDER = ['6060', '6063', '6082'];
