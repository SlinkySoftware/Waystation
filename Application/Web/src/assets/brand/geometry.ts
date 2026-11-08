// SPDX-License-Identifier: GPL-3.0-or-later
// Waystation brand geometry (UI_DESIGN §8): the single source for the BrandMark component and for
// scripts/gen-brand.mjs, which writes the master SVGs next to this file and the favicon set in public/.
// Plain erasable TypeScript with no imports so Node can run it directly (type stripping).

/** The mark: a waypoint marker whose head is a hexagon (the package seen end-on) holding a stack of layers. */
export const MARK = {
  viewBox: '0 0 32 32',
  /** Marker silhouette: a regular hexagon (centre 16,13, radius 11) whose lower vertex is drawn out to a tip. */
  marker: 'M16 2 25.5 7.5V18.5L16 29 6.5 18.5V7.5Z',
  /** Rounds the marker's corners (same paint as the fill). */
  markerStroke: 3,
  /** Top slab of the stack (filled and stroked so its corners are round). */
  slab: 'M16 6.2 21.4 9.3 16 12.4 10.6 9.3Z',
  slabStroke: 2,
  /** The layers under the slab. */
  layers: ['M10.6 13.3 16 16.4 21.4 13.3', 'M10.6 17.3 16 20.4 21.4 17.3'],
  layerStroke: 2.2,
} as const

/**
 * The same mark tuned for 16 px (favicon.svg, favicon-16.png and BrandMark below 20 px): one layer instead of two,
 * so slab and layer stay a clear pixel apart on the 2-unit pixel grid.
 */
export const MARK_SMALL = {
  ...MARK,
  slab: 'M16 5.5 21.5 8.75 16 12 10.5 8.75Z',
  slabStroke: 1,
  layers: ['M10.5 15.5 16 18.75 21.5 15.5'],
  layerStroke: 2.6,
} as const

/** Brand colours (mirrors --brand-500 in src/css/app.css, light and dark) and the glyph colours used on them. */
export const COLORS = {
  brand: '#0f766e',
  brandDark: '#2ebfae',
  onBrand: '#ffffff',
  onBrandDark: '#0b1f1d',
  ink: '#0f172a',
} as const

/**
 * The wordmark: "waystation" in a monoline geometric lowercase drawn for this mark (round caps and joins, one
 * stroke weight, bowls on a 16-unit circle). Centre-line paths on a 40-unit cap box; baseline at y=38.
 */
export const WORDMARK = {
  stroke: 4,
  /** Ink box: ascender top (t, y=10) to descender bottom (y, y=50) plus 2 units, x-height band 20-40. */
  top: 8,
  height: 44,
  width: 215,
  paths: [
    'M2 22 7.3 38 12.6 22 17.9 38 23.2 22',
    'M47.7 22V38M47.7 30A8 8 0 1 0 31.7 30 8 8 0 1 0 47.7 30',
    'M56.2 22 65.4 38M72.2 22 61.2 48',
    'M92.3 24.4C91 22.8 89.1 22 86.7 22 83.3 22 81 23.8 81 26.1 81 28.6 83.3 29.4 86.7 30 90.3 30.6 92.6 31.6 92.6 34 92.6 36.4 90.1 38 86.7 38 83.7 38 81.7 37 80.7 35.4',
    'M106.1 12V33A5 5 0 0 0 111.1 38H113.1M101.1 22H113.1',
    'M136.1 22V38M136.1 30A8 8 0 1 0 120.1 30 8 8 0 1 0 136.1 30',
    'M149.1 12V33A5 5 0 0 0 154.1 38H156.1M144.1 22H156.1',
    'M163.6 22V38',
    'M188.1 30A8 8 0 1 0 172.1 30 8 8 0 1 0 188.1 30',
    'M196.6 22V38M196.6 30A8 8 0 0 1 212.6 30V38',
  ] as readonly string[],
  /** The dot of the i (filled circle). */
  dot: { cx: 163.6, cy: 13.5, r: 2.6 },
} as const
