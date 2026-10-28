"""Build-time patch: stop Beets treating audio files as archives.

High-bitrate FLACs can contain the zip end-of-directory bytes by chance, so
zipfile.is_zipfile() says yes and the import fails ("Bad offset for central
directory"). Known audio extensions are never archives.
"""
import beets.importer.tasks

path = beets.importer.tasks.__file__
code = open(path, encoding="utf-8").read()
marker = "Do not treat known audio formats as archives"
target = "        if not os.path.isfile(path):\n            return False"
if marker not in code:
    if target not in code:
        raise SystemExit("beets changed: archive patch target not found, update patch_beets.py")
    code = code.replace(target, target + '''
        # ''' + marker + '''
        ext = os.path.splitext(os.fsdecode(path))[1].lower()
        if ext in {".mp3", ".flac", ".m4a", ".alac", ".ogg", ".opus", ".wav", ".aac", ".wma"}:
            return False''', 1)
    open(path, "w", encoding="utf-8").write(code)
print("beets archive patch applied")
