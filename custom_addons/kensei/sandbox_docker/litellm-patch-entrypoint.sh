#!/bin/sh

# Patch: fix iteration-during-modification bug in litellm callbacks
find /app/litellm/ -name '*.py' -exec grep -l 'for callback in litellm.callbacks:' {} \; 2>/dev/null | \
  while read -r f; do sed -i 's/for callback in litellm\.callbacks:/for callback in list(litellm.callbacks):/' "$f"; done

exec docker/prod_entrypoint.sh "$@"
