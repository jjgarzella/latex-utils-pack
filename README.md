# latex-utils

A Gas City **utility pack** of reusable LaTeX helpers — scripts and assets for
building, linting, and cleaning LaTeX projects across rigs (e.g. `my-paper-rig`).

It is asset-only: no agents, no named sessions. Import it alongside a
role-providing pack (like `gasvillage`).

## Layout

```
pack.toml              # pack manifest
assets/scripts/        # LaTeX helper scripts (build, clean, ...)
```

## Using it in a rig

Add an import to the rig in `city.toml`:

```toml
[[rigs]]
name = "my-paper-rig"
[rigs.imports.gasvillage]
source = "packs/gasvillage"
[rigs.imports.latex-utils]
source = "packs/latex-utils"
```

Then reference scripts by path, e.g. `{{.CityRoot}}/packs/latex-utils/assets/scripts/<script>.sh`.
