"""Clean up test pipeline artifacts"""
import shutil, os

base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
dirs_to_remove = [
    os.path.join(base, name)
    for name in ("_pipeline_test", "_pipeline_test2", "_pipeline_test3", "_pipeline_debug")
]

for d in dirs_to_remove:
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
        print(f"Removed: {d}")
    else:
        print(f"Not found: {d}")

print("Done")
