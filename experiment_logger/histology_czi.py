"""Read-only CZI inspection and bounded, full-resolution stitched OME-TIFF export."""
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
import math
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from .io import Cancelled, LoggerError, digest, signature

EXPORTER_VERSION = "1.0.0"
# Keep the loaded app's source fingerprint if its checkout is edited mid-export.
EXPORTER_SOURCE_SHA256 = digest(Path(__file__).read_bytes())
PIXEL_DTYPES = {"Gray8": "uint8", "Gray16": "uint16", "Gray32": "int32", "Gray32Float": "float32"}


def _stop(cancel):
    if cancel and cancel():
        raise Cancelled("Histology operation cancelled; use Recover for authorized transfers")


def _number(text):
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def inspect_czi(path, cancel=None):
    """Read headers, XML and tile indices only. No pixel reads or full-file hash."""
    from pylibCZIrw import czi
    path = Path(path)
    if path.suffix.lower() != '.czi' or not path.is_file():
        raise LoggerError('Choose an existing CZI file')
    before = signature(path)
    _stop(cancel)
    with czi.open_czi(str(path)) as doc:
        xml = doc.raw_metadata
        if len(xml) > 16*1024*1024:
            raise LoggerError('CZI metadata exceeds the supported 16 MB limit')
        tree = ET.fromstring(xml)
        for node in tree.iter():
            node.tag = node.tag.rsplit('}', 1)[-1]
        meta = tree.find('Metadata')
        if meta is None:
            raise LoggerError('CZI has no readable image metadata')
        image = meta.find('Information/Image')
        if image is None:
            raise LoggerError('CZI has no image information')
        original_time = image.findtext('AcquisitionDateAndTime') or meta.findtext('Information/Document/CreationDate')
        local_time = None
        if original_time:
            try:
                stamp = datetime.fromisoformat(original_time.replace('Z', '+00:00'))
                if stamp.tzinfo is not None:
                    local_time = stamp.astimezone(ZoneInfo('America/New_York')).isoformat()
            except ValueError:
                pass
        bounds = {k: [int(v[0]), int(v[1])] for k,v in doc.total_bounding_box.items()}
        scenes = [{'index': int(i), 'x': rect.x, 'y': rect.y, 'width': rect.w, 'height': rect.h}
                  for i,rect in sorted(doc.scenes_bounding_rectangle_no_pyramid.items())]
        if not scenes:
            rect = doc.total_bounding_rectangle
            scenes = [{'index': None, 'x': rect.x, 'y': rect.y, 'width': rect.w, 'height': rect.h}]
        tiles, subblocks = set(), 0
        def count(_index, info):
            nonlocal subblocks
            _stop(cancel)
            rect, size = info.logicalRect, info.physicalSize
            if rect.w == size.w and rect.h == size.h:
                subblocks += 1
                coord = info.coordinate.to_dict()
                if info.is_mindex_valid():
                    tiles.add((coord.get('S', 0), int(info.mIndex)))
            return True
        doc.enumerate_subblocks(count)
        scale = {}
        for node in meta.findall('Scaling/Items/Distance'):
            value = _number(node.findtext('Value'))
            if value is not None and value > 0:
                scale[node.attrib['Id'].lower()] = value*1e6
        channels = []
        for i,node in enumerate(image.findall('Dimensions/Channels/Channel')):
            channels.append({'index': i, 'name': node.get('Name') or f'Channel {i}',
                'fluorophore_preset': node.findtext('Fluor'), 'color': node.findtext('Color'),
                'laser_wavelength_nm': _number(node.findtext('LightSourcesSettings/LightSourceSettings/Wavelength')),
                'dye_excitation_wavelength_nm': _number(node.findtext('ExcitationWavelength')),
                'dye_emission_wavelength_nm': _number(node.findtext('EmissionWavelength'))})
        cstart, cend = bounds['C']
        for i,channel in enumerate(channels):
            channel['index'] = cstart+i
        if len(channels) != cend-cstart:
            channels = [{'index': i, 'name': f'Channel {i}', 'fluorophore_preset': None, 'color': None,
                         'laser_wavelength_nm': None, 'dye_excitation_wavelength_nm': None,
                         'dye_emission_wavelength_nm': None} for i in range(cstart,cend)]
        objective_id = image.find('ObjectiveSettings/ObjectiveRef')
        objective = next((o for o in meta.findall('Information/Instrument/Objectives/Objective')
                          if objective_id is not None and o.get('Id') == objective_id.get('Id')), None)
        result = {'reader_version': version('pylibCZIrw'), 'acquisition_time_original': original_time,
            'acquisition_time_local': local_time, 'bounds': bounds, 'scenes': scenes,
            'declared_dimensions':{n.tag[4:]:int(n.text) for n in image if n.tag.startswith('Size')
                                   and n.text and n.text.isdecimal()},
            'pixel_types': {str(i): t for i,t in doc.pixel_types.items()}, 'pixel_size_um': scale,
            'channels': channels, 'tile_count': len(tiles) if tiles else None,
            'tile_grids': [{'name': t.get('Name'), 'rows': _number(t.findtext('Rows')),
                            'columns': _number(t.findtext('Columns'))} for t in meta.findall('.//TileRegion')],
            'full_resolution_subblocks': subblocks,
            'objective': ({'name': objective.get('Name'), 'magnification': _number(objective.findtext('NominalMagnification')),
                           'numerical_aperture': _number(objective.findtext('LensNA'))} if objective is not None else None)}
    if before != signature(path):
        raise LoggerError('CZI changed during inspection; wait for acquisition to finish')
    return result


def export_spec(info):
    """Reject unsupported axes/types before moving a file selected for export."""
    if any(end-start > 1 for axis,(start,end) in info['bounds'].items() if axis not in ('X','Y','Z','C','T')):
        raise LoggerError('TIFF export supports XYZCT and separate scenes; this file has another varying axis')
    types = set(info['pixel_types'].values())
    if len(types) != 1 or next(iter(types)) not in PIXEL_DTYPES:
        raise LoggerError('TIFF export needs consistent grayscale channels; this pixel format is not supported')
    dtype = PIXEL_DTYPES[next(iter(types))]
    shape = [info['bounds'][a][1]-info['bounds'][a][0] for a in ('T','Z','C')]
    if any(n <= 0 for n in shape) or any(s['width'] <= 0 or s['height'] <= 0 for s in info['scenes']):
        raise LoggerError('CZI contains empty dimensions')
    return dtype, shape


def export_stitched(source, destination, info, *, progress=None, cancel=None):
    """Stream 512-pixel tiles into BigTIFF, and compare written pixel digests.

    Overlaps use libCZI's highest-M-index composition, not intensity blending.
    Each scene is a separate OME image series; all Z/C/T planes are retained.
    """
    import hashlib
    import numpy as np
    import tifffile
    from pylibCZIrw import czi
    dtype, leading = export_spec(info)
    before = signature(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The journal owns this exact partial path. The published TIFF is never replaced.
    if destination.exists():
        raise LoggerError('Export destination exists; nothing overwritten')
    tile = 512
    itemsize = np.dtype(dtype).itemsize
    total = sum(math.prod(leading)*s['width']*s['height']*itemsize for s in info['scenes'])
    completed = 0
    expected = []
    with czi.open_czi(str(source)) as doc, tifffile.TiffWriter(destination, bigtiff=True, ome=True) as writer:
        for scene in info['scenes']:
            hashes = []
            def tiles():
                nonlocal completed
                for t in range(*info['bounds']['T']):
                    for z in range(*info['bounds']['Z']):
                        for c in range(*info['bounds']['C']):
                            h = hashlib.sha256()
                            # Digest is tile ordered, using only real edge pixels.
                            for y in range(0,scene['height'],tile):
                                for x in range(0,scene['width'],tile):
                                    _stop(cancel)
                                    width, height = min(tile,scene['width']-x), min(tile,scene['height']-y)
                                    block = doc.read(plane={'T':t,'Z':z,'C':c}, scene=scene['index'],
                                        roi=(scene['x']+x,scene['y']+y,width,height),zoom=1.0)
                                    if block.shape != (height,width,1) or block.dtype != np.dtype(dtype):
                                        raise LoggerError('CZI pixel dimensions or type changed during export')
                                    pixels = np.ascontiguousarray(block[...,0])
                                    h.update(pixels.tobytes())
                                    padded = np.zeros((tile,tile),dtype=dtype)
                                    padded[:height,:width] = pixels
                                    completed += pixels.nbytes
                                    if progress:
                                        progress(completed,total)
                                    yield padded
                            hashes.append(h.hexdigest())
            metadata = {'axes':'TZCYX', 'Name':f"Scene {scene['index']}" if scene['index'] is not None else 'Histology',
                'Channel': {'Name':[c['name'] for c in info['channels']]}}
            for axis in ('x','y','z'):
                if axis in info['pixel_size_um']:
                    metadata['PhysicalSize'+axis.upper()] = info['pixel_size_um'][axis]
                    metadata['PhysicalSize'+axis.upper()+'Unit'] = '\u00b5m'
            writer.write(tiles(),shape=(*leading,scene['height'],scene['width']),dtype=dtype,
                         tile=(tile,tile),photometric='minisblack',metadata=metadata)
            expected.append(hashes)
    if signature(source) != before:
        raise LoggerError('CZI changed during TIFF export')
    _stop(cancel)
    if hasattr(progress,'phase'):
        progress.phase('Verifying exported TIFF pixels',True)
    checked = 0
    with tifffile.TiffFile(destination) as output:
        if not output.is_ome or len(output.series) != len(info['scenes']):
            raise LoggerError('Exported OME-TIFF series metadata is incomplete')
        for index,(series,scene,hashes) in enumerate(zip(output.series,info['scenes'],expected)):
            if len(series.pages) != math.prod(leading) or series.dtype != np.dtype(dtype):
                raise LoggerError('Exported TIFF lost a plane or changed its pixel type')
            for page,wanted in zip(series.pages,hashes):
                h = hashlib.sha256()
                for pixels,indices,_shape in page.segments(maxworkers=1,buffersize=4*1024*1024):
                    _stop(cancel)
                    y,x = indices[2:4]
                    if pixels is None:
                        raise LoggerError('Exported TIFF has a missing tile')
                    block = np.ascontiguousarray(pixels[0,:min(tile,scene['height']-y),:min(tile,scene['width']-x),0])
                    h.update(block.tobytes())
                    checked += block.nbytes
                    if progress:
                        progress(checked,total)
                if h.hexdigest() != wanted:
                    raise LoggerError('Exported TIFF pixels do not match the stitched source pixels')
    return {'exporter_version':EXPORTER_VERSION,'exporter_source_sha256':EXPORTER_SOURCE_SHA256,
            'software_versions':{p:version(p) for p in ('pylibCZIrw','tifffile','numpy')},
            'format':'OME-BigTIFF','axes':'TZCYX','dtype':dtype,'zoom':1.0,'tile_size':[tile,tile],
            'overlap_method':'highest_m_index','pixel_verification':'all exported pixels matched',
            'scenes':info['scenes'],'uncompressed_pixel_bytes':total}
