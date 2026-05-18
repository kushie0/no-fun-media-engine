"""Script registry — metadata for JOBS menu display and duration estimates."""

__all__ = ['SCRIPT_REGISTRY']

SCRIPT_REGISTRY: dict[str, dict] = {
    'encode_quads':   {'label': 'Quadrant encode',      'est_minutes': 45,  'lane': 'GPU'},
    'export_clips':   {'label': 'Clip export',          'est_minutes': 12,  'lane': 'GPU'},
    'split_audio':    {'label': 'Audio channel split',  'est_minutes': 8,   'lane': 'CPU'},
    'detect_silence': {'label': 'Silence detection',    'est_minutes': 0.5, 'lane': 'CPU'},
    'generate_reel':  {'label': 'Reel generation',      'est_minutes': 30,  'lane': 'GPU'},
    'transcode_mp3':  {'label': 'MP3 transcode',        'est_minutes': 1,   'lane': 'CPU'},
    '_remaster':      {'label': 'Remaster',              'est_minutes': 15,  'lane': 'CPU'},
    '_sync_quads':    {'label': 'Sync quads',           'est_minutes': 3,   'lane': 'IO'},
    '_sync_audio':    {'label': 'Sync audio',           'est_minutes': 2,   'lane': 'IO'},
    '_sync_reel':     {'label': 'Sync reel',            'est_minutes': 2,   'lane': 'IO'},
    '_reupload':      {'label': 'Cloud upload',         'est_minutes': 5,   'lane': 'IO'},
    '_sync':          {'label': 'Sync performances',    'est_minutes': 2,   'lane': 'IO'},
    '_expire':        {'label': 'Expire cloud shares',  'est_minutes': 1,   'lane': 'IO'},
    '_expire_raw':    {'label': 'Expire raw files',     'est_minutes': 1,   'lane': 'IO'},
    '_scan':          {'label': 'File scan',            'est_minutes': 3,   'lane': 'IO'},
}
