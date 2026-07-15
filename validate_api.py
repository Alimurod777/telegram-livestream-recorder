"""Check Direction and Device from_raw mapping"""
from pytgcalls.types import Direction, Device
import inspect

# Check Direction.from_raw
print("Direction.from_raw source:")
print(inspect.getsource(Direction.from_raw))
print()

# Check Device.from_raw  
print("Device.from_raw source:")
print(inspect.getsource(Device.from_raw))
print()

# Check StreamDevice from ntgcalls
try:
    from ntgcalls import StreamDevice, StreamMode
    print("StreamDevice values:", [(e.name, e.value) for e in StreamDevice])
    print("StreamMode values:", [(e.name, e.value) for e in StreamMode])
except Exception as e:
    print("ntgcalls import error:", e)
    # try raw
    import ntgcalls
    print("ntgcalls dir:", [x for x in dir(ntgcalls) if not x.startswith('_')])
