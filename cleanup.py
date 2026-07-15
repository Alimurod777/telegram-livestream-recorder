"""Clean up test pipeline artifacts"""
import shutil, os

dirs_to_remove = [
    r"c:\Users\User\loyiha\record\recordings\_pipeline_test",
    r"c:\Users\User\loyiha\record\recordings\_pipeline_test2",
    r"c:\Users\User\loyiha\record\recordings\_pipeline_test3",
    r"c:\Users\User\loyiha\record\recordings\_pipeline_debug",
]

for d in dirs_to_remove:
    if os.path.exists(d):
        shutil.rmtree(d, ignore_errors=True)
        print(f"Removed: {d}")
    else:
        print(f"Not found: {d}")

print("Done")
