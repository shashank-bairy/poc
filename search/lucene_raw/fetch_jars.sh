#!/usr/bin/env bash
# Pull the Lucene jars straight from Maven Central. No Maven, no Gradle --
# the point of this stage is to see Lucene with nothing on top of it.
set -euo pipefail
cd "$(dirname "$0")"

LUCENE=9.11.1
GSON=2.11.0
mkdir -p lib

fetch() {  # group-path artifact version
  local path="$1" artifact="$2" version="$3"
  local jar="$artifact-$version.jar"
  [ -f "lib/$jar" ] && return
  echo "  $jar"
  curl -fsSL -o "lib/$jar" "https://repo1.maven.org/maven2/$path/$artifact/$version/$jar"
}

echo "==> fetching jars into lucene_raw/lib"
for a in lucene-core lucene-analysis-common lucene-queryparser lucene-queries \
         lucene-highlighter lucene-memory lucene-facet; do
  fetch "org/apache/lucene" "$a" "$LUCENE"
done
# Only non-Lucene dependency: JSON in and out, so the Python side can drive it.
fetch "com/google/code/gson" "gson" "$GSON"

echo "==> done"
