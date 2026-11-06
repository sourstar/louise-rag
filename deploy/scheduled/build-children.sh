#!/bin/bash
# AFTER (scheduling-log "the parse pool keeps its children", 2026-10-25): the live build
# container's parse children and its cpu every 60 s, for as long as it runs
C=louise-wikipedia-night
while docker ps -q --filter "name=^${C}$" | grep -q .; do
  echo "$(date +%T) children=$(docker top "$C" -eo pid,args 2>/dev/null | grep -c spawn_main) cpu=$(docker stats --no-stream "$C" --format '{{.CPUPerc}}')"
  sleep 60
done >> /root/build-children.log
