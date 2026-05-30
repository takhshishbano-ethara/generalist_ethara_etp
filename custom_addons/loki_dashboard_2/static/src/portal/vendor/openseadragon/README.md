# OpenSeadragon (vendored)

The WSI viewer at `/loki2` loads OpenSeadragon from this directory rather than a
CDN. Drop the release files here so the portal can serve them as static assets.

## Required files

```
vendor/openseadragon/
    openseadragon.min.js
    openseadragon.css           # optional, not required by core
    images/                     # navigator + nav button sprites
        button_grouphover.png
        button_hover.png
        button_pressed.png
        button_rest.png
        fullpage_grouphover.png
        fullpage_hover.png
        fullpage_pressed.png
        fullpage_rest.png
        home_grouphover.png
        home_hover.png
        home_pressed.png
        home_rest.png
        next_grouphover.png
        next_hover.png
        next_pressed.png
        next_rest.png
        previous_grouphover.png
        previous_hover.png
        previous_pressed.png
        previous_rest.png
        rotateleft_grouphover.png
        rotateleft_hover.png
        rotateleft_pressed.png
        rotateleft_rest.png
        rotateright_grouphover.png
        rotateright_hover.png
        rotateright_pressed.png
        rotateright_rest.png
        zoomin_grouphover.png
        zoomin_hover.png
        zoomin_pressed.png
        zoomin_rest.png
        zoomout_grouphover.png
        zoomout_hover.png
        zoomout_pressed.png
        zoomout_rest.png
```

## Download

Grab the latest release from <https://openseadragon.github.io/#download>
(version 4.1.0 or newer recommended), unzip, and copy the contents of
`openseadragon-bin-<version>/` into this folder.

The portal template references `prefixUrl: "/loki_dashboard_2/static/src/portal/vendor/openseadragon/images/"`
when initialising the viewer.
