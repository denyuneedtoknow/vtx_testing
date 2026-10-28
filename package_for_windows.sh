#!/bin/zsh
# Create a small source archive for transfer from macOS to a Windows build machine.
set -euo pipefail

project_dir=${0:A:h}
archive_dir="$project_dir/dist"
archive_path="$archive_dir/VTX-Testing-source.zip"
staging_dir=$(mktemp -d)
bundle_dir="$staging_dir/VTX-Testing"

cleanup() {
  rm -rf "$staging_dir"
}
trap cleanup EXIT

mkdir -p "$bundle_dir" "$archive_dir"

cp "$project_dir/README.md" "$bundle_dir/"
cp "$project_dir/build.ps1" "$bundle_dir/"
cp "$project_dir/requirements.txt" "$bundle_dir/"
cp "$project_dir/vtx_power_logger.py" "$bundle_dir/"
cp "$project_dir/vtx_msp_probe.py" "$bundle_dir/"
cp "$project_dir/vtx_profiles.py" "$bundle_dir/"
cp "$project_dir/measurement_path.py" "$bundle_dir/"
cp "$project_dir/vtx_a1_measure.py" "$bundle_dir/"
cp "$project_dir/vtx_three_point_test.py" "$bundle_dir/"
cp "$project_dir/vtx_power_table_measure.py" "$bundle_dir/"
cp "$project_dir/vtx_tester_gui.py" "$bundle_dir/"
cp "$project_dir/vtx_tester_gui_qt.py" "$bundle_dir/"
cp "$project_dir/plot_vtx_power_table.py" "$bundle_dir/"
cp "$project_dir/compare_vtx_power_tables.py" "$bundle_dir/"
cp -R "$project_dir/configs" "$bundle_dir/"

rm -f "$archive_path"
# COPYFILE_DISABLE prevents macOS from adding __MACOSX and ._ metadata files.
(
  cd "$staging_dir"
  COPYFILE_DISABLE=1 /usr/bin/zip -r -X "$archive_path" "VTX-Testing" >/dev/null
)

print "Created: $archive_path"
