import sys
print("exec:", sys.executable)
print("path0:", sys.path[0])
print("site:", [p for p in sys.path if "site-packages" in p][:3])
try:
    import ruamel.yaml  # noqa: F401
    print("ruamel: ok")
except Exception as e:
    print("ruamel: FAIL", e)
