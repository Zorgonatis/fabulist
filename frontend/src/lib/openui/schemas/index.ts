// Public entry point for the VTK flavor schemas (W2.4a).
//
// The narrator-side validation gate imports the zod schemas from here; a plain
// JSON mirror lives beside this file at `./flavors.json` for runtimes without a
// JS toolchain.
export * from './flavors';