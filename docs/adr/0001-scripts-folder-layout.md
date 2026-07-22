# Scripts folder grouped by product

Scripts used to live flat under `scripts/`, which made the POV Archive, FACEIT helpers, uploads, and the new Highlight Reel path hard to navigate. We reorganized into `scripts/{pov,overlay,faceit,highlights,upload,hf,misc}/` and update all call sites (pipeline subprocess paths, backlog `pipeline_cmd`, docs, tests) rather than leaving root shims. Import discovery goes through `scripts/_pathsetup.ensure()`.
