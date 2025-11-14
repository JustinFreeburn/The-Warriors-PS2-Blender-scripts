

"""
Blender importer for Warriors PS2-style RenderWare DFF (vertices + triangles only)

Usage:
 - Paste into Blender Text Editor, set DFF_PATH, and Run Script.
 - Creates one mesh object per material split containing vertex positions and triangles.

Notes:
 - This tries to follow the reading logic in the C# files you shared:
   BinMeshPlg.cs, NativeDataPlg.cs, NativeDataPlgStructure.cs
 - Only vertex format handled for now: DMATag 0x6D008000 (4 x Int16).
 - Triangles are reconstructed as in your C# (triangle-strip style with duplicate checks).
 - Endianness: assumes little-endian (same as your C# BinaryReader on typical systems).
"""

import bpy
import struct
import os
from mathutils import Vector

# ---------- USER CONFIG ----------
DFF_PATH = r"C:\Warriors\dff\68AD0821.dff"  # set to full path of .dff or leave empty and input path when prompted
endian = '<'   # little-endian; set '>' for big-endian if needed
# ----------------------------------

# RenderWare Section IDs (from your RenderWareEnumerator.cs)
BINMESH_PLG_ID = 0x50E
NATIVE_DATA_PLG_ID = 0x510
STRUCT_ID = 0x1

# DMATag values (only the one we parse now)
DMATAG_VERTEX_4_INT16 = 0x6D008000

# Vertex scaling
SCALE = 1.0 / 128.0

# --- small binary helper
class BR:
    def __init__(self, f):
        self.f = f
    def pos(self): return self.f.tell()
    def seek(self, offset, whence=0): self.f.seek(offset, whence)
    def read_i32(self): return struct.unpack(endian + "i", self.f.read(4))[0]
    def read_u32(self): return struct.unpack(endian + "I", self.f.read(4))[0]
    def read_i16(self): return struct.unpack(endian + "h", self.f.read(2))[0]
    def read_u8(self): return struct.unpack("B", self.f.read(1))[0]
    def read_i8(self): return struct.unpack("b", self.f.read(1))[0]
    def read_bytes(self, n): return self.f.read(n)

# --- scanning helpers
def find_chunk_positions(f, target_id):
    """
    Return list of offsets where target_id occurs as a 4-byte little-endian int.
    We assume chunk header format: [SectionID (int32)][iSectionSize (int32)][iRenderWareVersion (int32)]...
    """
    matches = []
    f.seek(0, 2)
    filesize = f.tell()
    f.seek(0, 0)
    chunk = f.read()
    # search for 4-byte pattern
    pattern = struct.pack(endian + "i", target_id)
    start = 0
    while True:
        idx = chunk.find(pattern, start)
        if idx == -1:
            break
        matches.append(idx)
        start = idx + 4
    return matches

# --- parse BinMeshPlg matching BinMeshPlg.Read()
def parse_binmesh_plg(br, offset):
    """
    Expect offset points to SectionID (already matched). Read section size & contents like C#.
    Returns dictionary with iMaterialSplitCount and list of binMeshes { iIndexCount, iMaterialIndex, VertexIndices(optional) }
    """
    br.seek(offset, 0)
    section_id = br.read_i32()           # should equal BINMESH_PLG_ID
    iSectionSize = br.read_i32()
    iRenderWareVersion = br.read_i32()
    data_start = br.pos()
    # If section size small (==12) C# just skips
    if iSectionSize == 12:
        return {"iMaterialSplitCount": 0, "binMeshes": []}
    # now read fields
    iFaceType = br.read_i32()
    iMaterialSplitCount = br.read_i32()
    iTotalFaceCount = br.read_i32()
    binMeshes = []
    for i in range(iMaterialSplitCount):
        iIndexCount = br.read_i32()
        iMaterialIndex = br.read_i32()
        mesh_entry = {"iIndexCount": iIndexCount, "iMaterialIndex": iMaterialIndex, "VertexIndices": None}
        # C# reads vertex indices only if section size != (12 + (8 * iMaterialSplitCount))
        if iSectionSize != (12 + (8 * iMaterialSplitCount)):
            # read iIndexCount int32s
            idxs = []
            for _ in range(iIndexCount):
                try:
                    idxs.append(br.read_i32())
                except Exception:
                    break
            mesh_entry["VertexIndices"] = idxs
        binMeshes.append(mesh_entry)
    return {"iMaterialSplitCount": iMaterialSplitCount, "binMeshes": binMeshes, "section_size": iSectionSize, "section_offset": offset}

# --- parse NativeDataPLG struct and extract PS2 native data per C# logic (but only vertex type 4×int16 implemented)
def parse_native_data_plg(br, offset, binmesh):
    """
    Parse NativeDataPLG at offset and return list of material_splits with Vertices and Triangles.
    Follows the Read() -> ReadPS2NativeDataPlg() logic from NativeDataPlg.* files you provided.
    """
    br.seek(offset, 0)
    _sid = br.read_i32()  # should be NATIVE_DATA_PLG_ID
    iSectionSize = br.read_i32()
    iRenderWareVersion = br.read_i32()
    # Next should be a Struct id (0x1)
    maybe_struct = br.read_i32()
    if maybe_struct != STRUCT_ID:
        raise Exception("NativeDataPLG did not contain expected Struct ID at offset %d." % br.pos())
    # Next, the struct (NativeDataPlgStructure) itself begins: it has its own iSectionSize & iRenderWareVersion per C# Read()
    struct_section_size = br.read_i32()
    struct_rwv = br.read_i32()
    # If struct_section_size == 4, the C# code skips -- treat as empty
    if struct_section_size == 4:
        return []
    # read platform
    uiPlatform = br.read_u32()
    # only handle Playstation2ClumpNative (0x4)
    if uiPlatform != 0x4:
        raise Exception("Unsupported platform 0x%X" % uiPlatform)
    material_splits = []
    # Now for each binmesh split, parse split section:
    matcount = binmesh["iMaterialSplitCount"]
    for split_idx in range(matcount):
        uiSplitSize = br.read_u32()
        br.seek(4, 1)  # SeekCurrent(4) in C#
        lSectionStart = br.pos()
        br.seek(4, 1)
        uiDMASize = br.read_u32() * 16
        br.seek(lSectionStart, 0)
        lDMASectionEnd = lSectionStart + uiDMASize
        lSectionEnd = lSectionStart + uiSplitSize
        # prepare material split container
        ms = {"iMaterialIndex": binmesh["binMeshes"][split_idx]["iMaterialIndex"], "Vertices": None, "Triangles": []}
        bReachedEnd = False
        # iterate DMA entries
        while (br.pos() < lDMASectionEnd) and (not bReachedEnd):
            br.seek(3, 1)
            # protect against EOF
            try:
                bDMAId = br.read_u8()
            except Exception:
                break
            uiDataOffset = br.read_u32()
            br.seek(4, 1)
            uiDataType = br.read_u32()
            lDataPosition = lSectionStart + (uiDataOffset * 16)
            uiDataType &= 0xFF00FFFF
            if bDMAId == 0x30:
                # vertex data type we handle: 0x6D008000 (4 x Int16)
                if uiDataType == DMATAG_VERTEX_4_INT16:
                    # jump to lDataPosition and read binmesh.iIndexCount vertices of 4xint16
                    oldpos = br.pos()
                    br.seek(lDataPosition, 0)
                    idxcount = binmesh["binMeshes"][split_idx]["iIndexCount"]
                    verts = []
                    for vi in range(idxcount):
                        # each entry: int16 x,y,z and skip 2 bytes
                        try:
                            x = float(br.read_i16())
                            y = float(br.read_i16())
                            z = float(br.read_i16())
                        except Exception:
                            break
                        br.seek(2, 1)  # skip padding
                        verts.append(Vector((x * SCALE, y * SCALE, z * SCALE)))
                    ms["Vertices"] = verts
                    ms["HasVertexData"] = True
                    br.seek(oldpos, 0)
                # skip the 16 bytes after processing a 0x30 block (C# reader.SeekCurrent(16))
                br.seek(16, 1)
            elif bDMAId == 0x10:
                bReachedEnd = True
            else:
                # unknown DMA id — move on (no explicit skip in C# other than loop remaining)
                pass
        # reconstruct triangles as in C# NativeDataPlgStructure
        if ms.get("HasVertexData", False) and ms["Vertices"] is not None:
            vc = len(ms["Vertices"])
            tris = []
            for i in range(2, vc):
                v_i = ms["Vertices"][i]
                v_i1 = ms["Vertices"][i-1]
                v_i2 = ms["Vertices"][i-2]
                # duplicate-vertex checks (exact equality)
                if (v_i.x == v_i1.x and v_i.y == v_i1.y and v_i.z == v_i1.z) or \
                   (v_i.x == v_i2.x and v_i.y == v_i2.y and v_i.z == v_i2.z) or \
                   (v_i1.x == v_i2.x and v_i1.y == v_i2.y and v_i1.z == v_i2.z):
                    continue
                if (i % 2) == 0:
                    tris.extend([i, i-2, i-1])
                else:
                    tris.extend([i-1, i-2, i])
            ms["Triangles"] = tris
        material_splits.append(ms)
        # Seek to lSectionEnd
        br.seek(lSectionEnd, 0)
    return material_splits

# --- Blender mesh creation
def create_mesh_object(name, verts, tris):
    if not verts or not tris:
        print("Skipping empty mesh:", name)
        return None
    mesh = bpy.data.meshes.new(name + "_mesh")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    # create faces from tris (list of vertex indices)
    faces = [tuple(tris[i:i+3]) for i in range(0, len(tris), 3)]
    mesh.from_pydata([ (v.x, v.y, v.z) for v in verts ], [], faces)
    mesh.update()
    return obj

# --- main import function
def import_dff(dff_path):
    if not os.path.exists(dff_path):
        print("File not found:", dff_path)
        return
    with open(dff_path, "rb") as f:
        br = BR(f)
        # find BinMeshPlg and NativeDataPLG header offsets
        binmesh_positions = find_chunk_positions(f, BINMESH_PLG_ID)
        native_positions = find_chunk_positions(f, NATIVE_DATA_PLG_ID)
        if not binmesh_positions:
            print("No BinMeshPLG (0x50E) chunk found.")
            return
        if not native_positions:
            print("No NativeDataPLG (0x510) chunk found.")
            return
        # choose first occurrences (common case)
        binmesh_off = binmesh_positions[0]
        native_off = native_positions[0]
        print("BinMeshPLG at", binmesh_off, "NativeDataPLG at", native_off)
        binmesh = parse_binmesh_plg(br, binmesh_off)
        splits = parse_native_data_plg(br, native_off, binmesh)
        created = []
        for i, s in enumerate(splits):
            name = f"dff_split_{i}_mat{ s.get('iMaterialIndex') }"
            obj = create_mesh_object(name, s.get("Vertices"), s.get("Triangles"))
            if obj:
                created.append(obj)
        print("Imported", len(created), "meshes")

# entrypoint
def main():
    global DFF_PATH
    if not DFF_PATH:
        import sys
        try:
            print("Enter path to .dff file:")
            DFF_PATH = input().strip()
        except Exception:
            print("No path provided, aborting.")
            return
    import_dff(DFF_PATH)

if __name__ == "__main__":
    main()
