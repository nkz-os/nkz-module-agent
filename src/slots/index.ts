/**
 * Slot definitions — declare which host slots this module occupies.
 *
 * Available slots:
 *   map-layer      — overlay or toolbar button on the 3D map
 *   layer-toggle   — toggle entry in the layer panel
 *   context-panel  — side panel shown when an entity is selected
 *   bottom-panel   — tabbed panel at the bottom of the viewer
 *   entity-tree    — context menu entry in the entity tree
 *   dashboard-widget — card in the tenant dashboard
 *
 * This module occupies none of them — it ships a standalone page
 * (src/App.tsx, routed via `route` in Module.tsx's defineModule()) rather
 * than a viewer panel. Register a component here (and in manifest.json's
 * matching `slots` key) if that changes.
 */
import type { ModuleViewerSlots } from '@nekazari/sdk';

export const moduleSlots: ModuleViewerSlots = {
  'map-layer': [],
  'layer-toggle': [],
  'context-panel': [],
  'bottom-panel': [],
  'entity-tree': [],
  'dashboard-widget': [],
};
