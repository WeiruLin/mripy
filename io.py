#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import, unicode_literals
import sys, os, subprocess
import re, glob, shlex, shutil, tempfile
import collections, itertools, copy
import random, string
from os import path
from datetime import datetime
import numpy as np
from . import six, utils, afni
# For accessing NIFTI files
try:
    import nibabel
except ImportError:
    print('You may need to install "nibabel" to read/write NIFTI (*.nii) files.')
try:
    from lxml import etree
except ImportError:
    print('You may need to install "lxml" to read/write niml datasets (*.niml.dset).')

# Timestamp
def _timestamp(dt):
    '''
    Work-around for python 2.7 not having dt.timestamp() yet.
    http://stackoverflow.com/questions/11743019/convert-python-datetime-to-epoch-with-strftime
    '''
    return (dt - datetime(1970,1,1)).total_seconds()


def hms2dt(hms, date=None, timestamp=False):
    '''
    Convert time string in hms format to datetime object.

    `hms` is like "102907.165000". This format is used in dicom header.
    '''
    if date is None:
        date = '19700101'
    dt = datetime.strptime(date+hms, '%Y%m%d%H%M%S.%f')
    return _timestamp(dt) if timestamp else dt


def mmn2dt(mmn, date=None, timestamp=False):
    '''
    Convert time string in mmn format to datetime object.

    `mmn` is "msec since midnight", like "37747165". This format is used in
    physiological measurement log file.
    '''
    if date is None:
        date = '19700101'
    t = datetime.utcfromtimestamp(float(mmn)/1000)
    d = datetime.strptime(date, '%Y%m%d')
    dt = datetime.combine(d.date(), t.time())
    return _timestamp(dt) if timestamp else dt


# Physiological data
def _parse_physio_raw(fname):
    # print('Parsing "{0}"...'.format(fname))
    n_pre = {'ecg': 5, 'ext': 4, 'puls': 4, 'resp': 4}
    with open(fname, 'r') as fin:
        info = {}
        ch = path.splitext(fname)[1][1:]
        info['file'] = path.realpath(fname)
        info['channel'] = ch
        lines = fin.read().splitlines() # Without \n unlike fin.readlines()
        if len(lines) == 0:
            print('*+ WARNING: "{0}" seems be empty...'.format(fname), file=sys.stderr)
            return None
        k = 0
        # Data line(s)
        while lines[k][-4:] != '5003': # There can be more than one data lines
            k += 1
            if k >= len(lines): # The file does not contain 5003
                print('*+ WARNING: "{0}" might be broken...'.format(fname), file=sys.stderr)
                return None
        else:
            k += 1
            data_line = ''.join(lines[:k])
            info['messages'] = re.findall('\s5002\s(.+?)\s6002', data_line)
            data_line = re.sub('\s5002\s(.+?)\s6002', '', data_line) # Remove messages inserted between 5002/6002
            info['rawdata'] = np.int_(data_line.split()[n_pre[ch]:-1])
        # Timing lines
        items = ['LogStartMDHTime', 'LogStopMDHTime', 'LogStartMPCUTime', 'LogStopMPCUTime']
        for item in items:
            while True:
                match = re.match('({0}):\s+(\d+)'.format(item), lines[k])
                k += 1
                if match:
                    info[match.group(1)] = int(match.group(2))
                    break
        return info


def parse_physio_file(fname, date=None):
    '''
    Implementation notes
    --------------------
    1. The first 4 (ext, puls, resp) or 5 (ecg) values are parameters (of
       unknown meanings).
    2. There can be multiple data lines, within which extra parameters is
       inclosed between 5002 and 6002, especially for ecg.
    3. The footer is inclosed between 5003 and 6003, following physiological
       data (and that's why the final data value always appears to be 5003).
    4. The MDH values are timestamps derived from the clock in the scanner (so
       do DICOM images), while the MPCU values are timestamps derived from the
       clock within the PMU recording system [1]. Use MDH time to synchronize
       physiological and imaging time series.
    5. The trigger values (5000) are "inserted" into the data, and have to be
       stripped out from the time series [1]. This fact is double checked by
       looking at the smooth trend of the puls waveform.
    6. The sampling rate is slightly (and consistently) slower than specified
       in the manual and in [1].

    Notes about timing
    ------------------
    The scanner clock is slightly faster than the wall clock so that 2 sec in
    real time is recorded as ~2.008 sec in the scanner, affacting both dicom
    header and physiological footer, even though the actual TR is precisely 2 s
    (as measured by timing the s triggers with psychtoolbox) and the actual
    sampling rate of physiological data is precisely 50 Hz (as estimated by
    dividing the total number of samples by the corrected recording duration).

    References
    ----------
    [1] https://cfn.upenn.edu/aguirre/wiki/public:pulse-oximetry_during_fmri_scanning
    '''
    # fs = {'ecg': 400, 'ext': 200, 'puls': 50, 'resp': 50}
    fs = {'ecg': 398.4, 'ext': 199.20, 'puls': 49.80, 'resp': 49.80}
    trig_value = 5000
    info = _parse_physio_raw(fname)
    if info is None:
        return None
    ch = info['channel']
    info['fs'] = fs[ch]
    info['start'] = mmn2dt(info['LogStartMDHTime'], date, timestamp=True)
    info['stop'] = mmn2dt(info['LogStopMDHTime'], date, timestamp=True)
    x = info['rawdata']
    if ch != 'ecg':
        # y = x.copy()
        y = x[x!=trig_value] # Strip trigger value (5000)
        trig = np.zeros_like(x)
        trig[np.nonzero(x==trig_value)[0]-1] = 1
        trig = trig[x!=trig_value]
    else:
        y = x[:len(x)//2*2].reshape(-1,2)
        trig = np.zeros_like(y)
    info['data'] = y
    info['trig'] = trig
    info['t'] = info['start'] + np.arange(len(y)) / fs[ch]
    try:
        assert(np.abs(info['t'][-1]-info['stop'])<2/info['fs']) # Allow 1 sampleish error
    except AssertionError as err:
        print('{0}: Last sample = {1}, stop = {2}, error = {3}'.format(
            info['channel'], info['t'][-1], info['stop'], info['t'][-1]-info['stop']))
        raise err
    return info


def parse_physio_files(fname, date=None, channels=None):
    '''
    '''
    if channels is None:
        channels = ['ecg', 'ext', 'puls', 'resp']
    stem = path.splitext(fname)[0]
    info = collections.OrderedDict()
    for ch in channels:
        info[ch] = parse_physio_file('.'.join((stem, ch)), date=date)
        if info[ch] is None:
            print('*+ WARNING: "{0}" info is missing. Skip "{1}"...'.format(ch, stem), file=sys.stderr)
            return None
    return info


def match_physio_with_series(physio_infos, series_infos, channel=None, method='cover'):
    if channel is None:
        channel = 'resp'
    physio_t = np.array([[p[channel]['start'], p[channel]['stop']] if p is not None else [0, 0] for p in physio_infos])
    physio = []
    series = []
    for k, s in enumerate(series_infos):
        if method == 'cover':
            p_idx = (physio_t[:,0] < s['start']) & (s['stop'] < physio_t[:,1])
            if np.any(p_idx):
                # If there is more than one (which should not be the case), use only the first one
                physio.append(physio_infos[np.nonzero(p_idx)[0][0]])
                series.append(s)
        elif method == 'overlap':
            p_idx = (physio_t[:,0] < s['stop']) & (s['start'] < physio_t[:,1]) # Thanks to Prof. Zhang Jun
            if np.any(p_idx):
                # If there is more than one (which should not be the case), use the one with largest overlap
                overlap = np.maximum(physio_t[p_idx,0], s['start']) - np.minimum(physio_t[p_idx,1], s['stop'])
                idx = np.nonzero(p_idx)[0][np.argmax(overlap)]
                physio.append(physio_infos[idx])
                series.append(s)
    return physio, series


def _print_physio_timing(pinfo, sinfo, channel, index=None):
    prefix = channel if index is None else '#{0} ({1})'.format(index, channel)
    print('{0}: pre={1:.3f}, scan={2:.3f}, post={3:.3f}, total={4:.3f}'.format(
        prefix, sinfo['start']-pinfo['start'], sinfo['stop']-sinfo['start'],
        pinfo['stop']-sinfo['stop'], pinfo['stop']-pinfo['start']))


def extract_physio(physio_file, dicom_file, TR=None, dummy=0, channels=['resp', 'puls'], verbose=1):
    sinfo = parse_series_info(dicom_file) if isinstance(dicom_file, six.string_types) else dicom_file
    pinfo = parse_physio_files(physio_file, date=sinfo['date']) if isinstance(physio_file, six.string_types) else physio_file
    res = []
    for ch in channels:
        info = pinfo[ch]
        t = info['t']
        valid = (t >= sinfo['start']+dummy*TR) & (t < sinfo['stop']) # Assume timestamp indicates the start of the volume
        res.append(info['data'][valid])
        if verbose:
            _print_physio_timing(info, sinfo, ch)
    return res


# DICOM
def parse_dicom_header(fname, fields=None):
    '''
    Execute afni command `dicom_hdr` to readout most useful info from dicom header.

    Parameters
    ----------
    fname : str
    fields : {field: (matcher, extracter(match))}
        You can require additional fields in dicom header to be parsed.
        - field : e.g., 'ImageTime'
        - matcher : e.g. r'ID Image Time//(\S+)'
        - extracter : e.g., lambda match: io.hms2dt(match.group(1), date='20170706', timestamp=True)
    '''
    # print(fname)
    header = collections.OrderedDict()
    lines = subprocess.check_output(['dicom_hdr', fname]).decode('utf-8').split('\n')
    k = 0
    try:
        while True:
            match = re.search(r'ID Acquisition Date//(\S+)', lines[k])
            k += 1
            if match:
                header['AcquisitionDate'] = match.group(1)
                break
        while True:
            match = re.search(r'ID Acquisition Time//(\S+)', lines[k])
            k += 1
            if match:
                header['AcquisitionTime'] = match.group(1) # This marks the start of a volume
                header['volume_time'] = hms2dt(header['AcquisitionTime'], date=header['AcquisitionDate'], timestamp=True)
                break
        while True:
            match = re.search(r'ACQ Scanning Sequence//(.+)', lines[k])
            k += 1
            if match:
                header['sequence_type'] = match.group(1).strip()
                break
        while True:
            match = re.search(r'ACQ Sequence Variant//(.+)', lines[k])
            k += 1
            if match:
                header['sequence_type'] = ' '.join((match.group(1).strip(), header['sequence_type']))
                break
        while True:
            match = re.search(r'ACQ MR Acquisition Type //(.+)', lines[k])
            k += 1
            if match:
                header['sequence_type'] = ' '.join((match.group(1).strip(), header['sequence_type']))
                break
        while True:
            match = re.search(r'ACQ Slice Thickness//(\S+)', lines[k])
            k += 1
            if match:
                header['resolution'] = [float(match.group(1))]
                break
        while True:
            match = re.search(r'ACQ Repetition Time//(\S+)', lines[k])
            k += 1
            if match:
                header['RepetitionTime'] = float(match.group(1)) # ms
                break
        while True:
            match = re.search(r'ACQ Echo Time//(\S+)', lines[k])
            k += 1
            if match:
                header['TE'] = float(match.group(1)) # ms
                break
        while True:
            match = re.search(r'ACQ Imaging Frequency//(\S+)', lines[k])
            k += 1
            if match:
                header['Larmor'] = float(match.group(1)) # MHz
                break
        while True:
            match = re.search(r'ACQ Echo Number//(\S+)', lines[k])
            k += 1
            if match:
                header['EchoNumber'] = int(match.group(1)) # For multi-echo images
                break
        while True:
            match = re.search(r'ACQ Magnetic Field Strength//(\S+)', lines[k])
            k += 1
            if match:
                header['B0'] = float(match.group(1)) # Tesla
                break
        while True:
            match = re.search(r'ACQ Pixel Bandwidth//(\S+)', lines[k])
            k += 1
            if match:
                header['BW'] = float(match.group(1)) # Hz/pixel
                break
        while True:
            match = re.search(r'ACQ Protocol Name//(.+)', lines[k])
            k += 1
            if match:
                header['ProtocolName'] = match.group(1).strip()
                break
        while True:
            match = re.search(r'ACQ Flip Angle//(\S+)', lines[k])
            k += 1
            if match:
                header['FlipAngle'] = float(match.group(1))
                break
        while True:
            match = re.search(r'ACQ SAR//(\S+)', lines[k])
            k += 1
            if match:
                header['SAR'] = float(match.group(1))
                break
        while True: # This field is optional
            match = re.search(r'0019 100a.+//\s*(\d+)', lines[k])
            k += 1
            if match:
                header['n_slices'] = int(match.group(1))
                break
            if lines[k].startswith('0020'):
                break
        while True:
            match = re.search(r'REL Study ID//(\d+)', lines[k])
            k += 1
            if match:
                header['StudyID'] = int(match.group(1)) # Study index
                break
        while True:
            match = re.search(r'REL Series Number//(\d+)', lines[k])
            k += 1
            if match:
                header['SeriesNumber'] = int(match.group(1)) # Series index
                break
        while True:
            match = re.search(r'REL Acquisition Number//(\d+)', lines[k])
            k += 1
            if match:
                header['AcquisitionNumber'] = int(match.group(1)) # Volume index
                break
        while True:
            match = re.search(r'REL Instance Number//(\d+)', lines[k])
            k += 1
            if match:
                header['InstanceNumber'] = int(match.group(1)) # File index (whether it is one volume or one slice per file)
                break
        while True:
            match = re.search(r'IMG Pixel Spacing//(\S+)', lines[k])
            k += 1
            if match:
                header['resolution'] = list(map(float, match.group(1).split('\\'))) + header['resolution']
                break
        while True: # This field is optional
            match = re.search(r'0051 1011.+//(\S+)', lines[k])
            k += 1
            if match:
                header['iPAT'] = match.group(1)
                break
            if lines[k].startswith('Group'):
                break
    except IndexError as error:
        print('** Failed to process "{0}"'.format(fname))
        raise error
    if fields is not None:
        for line in lines:
            for field, (matcher, extracter) in fields.items():
                match = re.search(matcher, line)
                if match:
                    header[field] = extracter(match)
                    break
    header['gamma'] = 2*np.pi*header['Larmor']/header['B0']
    return header


SERIES_PATTERN = r'.+?\.(\d{4})\.' # Capture series number
MULTI_SERIES_PATTERN = r'.+?\.(\d{4})\.(\d{4}).+(\d{8,})' # Capture series number, slice number, uid
MULTI_SERIES_PATTERN2 = r'.+?\.(\d{4})\.(\d{4}).+(\d{5,}\.\d{8,})' # Capture series number, slice number, uid (5-6.8-9)

def _sort_multi_series(files):
    '''
    Sort multiple series sharing the same series number into different studies.
    '''
    series = []
    timestamps = []
    infos = []
    for f in files:
        match = re.search(MULTI_SERIES_PATTERN2, f)
        # infos.append((f, int(match.group(2)), int(match.group(3))))
        infos.append((f, int(match.group(2)), float(match.group(3))))
    prev_slice = sys.maxsize
    for f, curr_slice, timestamp in sorted(infos, key=lambda x: x[-1]):
        if curr_slice <= prev_slice and (prev_slice == sys.maxsize or curr_slice in slices):
            # We meet a new sequence (including the first one).
            # Note that slices within a study are unique but may not be strictly ordered.
            # The first clause is a shortcut, and the second one is the real condition.
            series.append([])
            slices = set()
            timestamps.append(timestamp)
        series[-1].append(f)
        slices.add(curr_slice)
        prev_slice = curr_slice
    return series, timestamps


def sort_dicom_series(folder, series_pattern=SERIES_PATTERN):
    '''
    Parameters
    ----------
    folder : string
        Path to the folder containing all the *.IMA files.

    Returns
    -------
    studies : list of dicts
        [{'0001': [file0, file1, ...], '0002': [files], ...}, {study1}, ...]
    '''
    # Sort files into series
    files = sorted(glob.glob(path.join(folder, '*.IMA')))
    series = collections.OrderedDict()
    for f in files:
        filename = path.split(f)[1]
        match = re.search(series_pattern, filename)
        sn = match.group(1)
        if sn not in series:
            series[sn] = []
        series[sn].append(f)
    # Separate potentially multiple series sharing the same series number into different studies
    studies = None
    for s_idx, (sn, files) in enumerate(series.items()):
        subsets, timestamps = _sort_multi_series(files)
        if s_idx == 0:
            n_folders = len(subsets)
            # Note that if the first series is single, all series must be single.
            if n_folders == 1:
                studies = [series]
                break
            else:
                studies = [collections.OrderedDict() for k in range(n_folders)]
            for k, subset in enumerate(subsets):
                studies[k][sn] = subset
            start_times = timestamps
        else:
            # Handle the case when a later study has more series than earlier studies
            for k, subset in enumerate(subsets):
                kk = n_folders - 1
                while start_times[kk] > timestamps[k]:
                    kk -= 1
                studies[kk][sn] = subset
    return studies


def filter_dicom_files(files, series_numbers=None, instance_numbers=None, series_pattern=MULTI_SERIES_PATTERN):
    if isinstance(files, six.string_types) and path.isdir(files):
        files = glob.glob(path.join(files, '*.IMA'))
    if not isinstance(series_numbers, collections.Iterable):
        series_numbers = [series_numbers]
    if not isinstance(instance_numbers, collections.Iterable):
        instance_numbers = [instance_numbers]
    files = np.array(sorted(files))
    if len(files) == 0:
        return []
    infos = []
    for fname in files:
        filepath, filename = path.split(fname)
        match = re.match(series_pattern, filename)
        infos.append(list(map(int, match.groups()))) # series number, instance number, uid
    infos = np.array(infos)
    filtered = []
    if not series_numbers: # [] or None
        series_numbers = np.unique(infos[:,0])
    for series in series_numbers:
        if not instance_numbers:
            instance_numbers = np.unique(infos[infos[:,0]==series,1])
        for instance in instance_numbers:
            filtered.extend(files[(infos[:,0]==series)&(infos[:,1]==instance)])
    return filtered


def pares_slice_order(dicom_files):
    t = None
    if len(dicom_files) > 1:
        temp_dir = 'temp_pares_slice_order'
        os.makedirs(temp_dir)
        for k, f in enumerate(dicom_files[:2]):
            shutil.copyfile(f, path.join(temp_dir, '{0}.IMA'.format(k)))
        old_path = os.getcwd()
        try:
            os.chdir(temp_dir)
            afni.check_output('''Dimon -infile_pattern '*.IMA'
                -gert_create_dataset -gert_to3d_prefix temp -gert_quit_on_err''')
            res = afni.check_output(['3dAttribute', 'TAXIS_OFFSETS', 'temp+orig'])[-2]
            t = np.array(list(map(float, res.split())))
        finally:
            os.chdir(old_path)
            shutil.rmtree(temp_dir)
    if t is None:
        order = None
    elif np.all(np.diff(t) > 0):
        order = 'ascending'
    elif np.all(np.diff(t) < 0):
        order = 'descending'
    else:
        order = 'interleaved'
    return order, t


def parse_series_info(fname, volume_time=False, shift_time=None, series_pattern=SERIES_PATTERN, fields=None):
    if isinstance(fname, six.string_types): # A single file or a folder
        if path.isdir(fname):
            # Assume there is only one series in the folder, so that we only need to consider the first file.
            fname = sorted(glob.glob(path.join(fname, '*.IMA')))[0]
        # Select series by series number (this may fail if there is multi-series in the folder)
        filepath, filename = path.split(fname)
        match = re.match(series_pattern, filename)
        files = sorted(glob.glob(path.join(filepath, '{0}*.IMA'.format(match.group(0)))))
        findex = None
    else: # A list of files (e.g., as provided by sort_dicom_series)
        files = fname
        findex = 0
    info = collections.OrderedDict()
    if volume_time:
        parse_list = range(len(files))
    else:
        parse_list = [0, -1]
    headers = [parse_dicom_header(files[k], fields=fields) for k in parse_list]
    if headers[0]['StudyID'] != headers[-1]['StudyID']:
        # There are more than one series (from different studies) sharing the same series number
        if parse_list == [0, -1]:
            headers = [headers[0]] + [parse_dicom_header(f) for f in files[1:-1]] + [headers[-1]]
        if findex is None:
            findex = files.index(fname)
        selected = [k for k, header in enumerate(headers) if header['StudyID']==headers[findex]['StudyID']]
        files = [files[k] for k in selected]
        headers = [headers[k] for k in selected]
    info.update(headers[0])
    info['date'] = info['AcquisitionDate']
    info['first'] = headers[0]['volume_time']
    info['last'] = headers[-1]['volume_time']
    info['n_volumes'] = headers[-1]['AcquisitionNumber'] - headers[0]['AcquisitionNumber'] + 1
    info['TR'] = (info['last']-info['first'])/(info['n_volumes']-1) if info['n_volumes'] > 1 else None
    if shift_time == 'CMRR':
        shift_time = 0
        if info['TR'] is not None and 'n_slices' in info and np.mod(info['n_slices'], 2)==0:
            slice_order = pares_slice_order(files)[0]
            if slice_order == 'interleaved':
                shift_time = -info['TR']/2
    elif shift_time is None:
        shift_time = 0
    info['first'] += shift_time
    info['last'] += shift_time
    info['start'] = info['first']
    info['stop'] = (info['last'] + info['TR']) if info['TR'] is not None else info['last']
    if volume_time:
        info['t'] = np.array([header['volume_time'] for header in headers]) + shift_time
    info['files'] = [path.realpath(f) for f in files]
    info['headers'] = headers
    return info


def convert_dicom(folder, output_dir=None, prefix=None):
    if output_dir is None:
        output_dir = '.'
    output_dir = path.realpath(path.expanduser(output_dir))
    if not path.exists(output_dir):
        os.makedirs(output_dir)
    if prefix is None:
        prefix = path.split(folder)[1]
    old_path = os.getcwd()
    try:
        os.chdir(path.realpath(folder))
        with open('uniq_image_list.txt', 'w') as out_file:
            subprocess.check_call(['uniq_images'] + glob.glob('*.IMA'), stdout=out_file) # Prevent shell injection
        cmd = '''Dimon -infile_list uniq_image_list.txt
            -gert_create_dataset
            -gert_outdir "{0}"
            -gert_to3d_prefix "{1}"
            -overwrite
            -dicom_org
            -use_obl_origin
            -save_details Dimon.details
            -gert_quit_on_err
            '''.format(output_dir, prefix)
        afni.check_output(cmd)
    finally:
        os.chdir(old_path)


def convert_dicoms(folder, output_dir=None, prefix=None):
    '''
    Parameters
    ----------
    folder : str
        Root folder of rawdata, containing multiple sub-folders of *.IMA files.
        E.g., folder/anat, folder/func01, folder/func02, etc.
    output_dir : str
        Output directory for converted datasets, default is current directory.
        E.g., the output would look like
        output_dir/anat+orig, output_dir/func01+orig, etc.
    '''
    idx = 0
    for f in glob.glob(path.join(folder, '*')):
        if path.isdir(f) and len(glob.glob(path.join(f, '*.IMA'))) > 0:
            idx += 1
            convert_dicom(f, output_dir, prefix if prefix is None else '{0}{1:02d}'.format(prefix, idx))


# Volume data
def read_nii(fname, return_img=False):
    if fname[-4:] != '.nii':
        fname = fname + '.nii'
    img = nibabel.load(fname)
    vol = img.get_data()
    return (vol, img) if return_img else vol


def write_nii(fname, vol, base_img=None):
    if fname[-4:] != '.nii':
        fname = fname + '.nii'
    if base_img is None:
        affine = nibabel.affines.from_matvec(np.eye(3), np.zeros(3))
    elif isinstance(base_img, six.string_types):
        affine = nibabel.load(base_img).affine
    else:
        affine = base_img.affine
    img = nibabel.Nifti1Image(vol, affine)
    nibabel.save(img, fname)


def read_afni(fname, remove_nii=True, return_img=False):
    match = re.match('(.+)\+', fname)
    nii_fname = match.group(1) + '.nii'
    subprocess.check_call(['3dAFNItoNIFTI', '-prefix', nii_fname, fname])
    res = read_nii(nii_fname, return_img)
    if remove_nii:
        os.remove(nii_fname)
    return res


def write_afni(prefix, vol, base_img=None):
    nii_fname = prefix + '.nii'
    write_nii(nii_fname, vol, base_img)
    subprocess.check_call(['3dcopy', nii_fname, prefix+'+orig', '-overwrite'])
    os.remove(nii_fname)


def read_txt(fname, dtype=float, comment='#', delimiter=None, skiprows=0, return_comments=False):
    '''Read numerical array from text file, much faster than np.loadtxt()'''
    with open(fname, 'r') as fin:
        lines = fin.readlines()
    if return_comments:
        comments = [line for line in lines[skiprows:] if line.strip() and line.startswith(comment)]
    lines = [line for line in lines[skiprows:] if line.strip() and not line.startswith(comment)]
    n_cols = len(lines[0].split(delimiter))
    x = np.fromiter(itertools.chain.from_iterable(
        map(lambda line: line.split(delimiter), lines)), dtype=dtype).reshape(-1,n_cols)
    if return_comments:
        return x, comments
    else:
        return x


def read_asc(fname):
    '''Read FreeSurfer/SUMA surface (vertices and faces) in *.asc format.'''
    with open(fname, 'r') as fin:
        lines = fin.readlines()
    n_verts, n_faces = np.int_(lines[1].split())
    # verts = np.vstack(map(lambda line: np.float_(line.split()), lines[2:2+n_verts])) # As slow as np.loadtxt()
    # verts = np.float_(''.join(lines[2:2+n_verts]).split()).reshape(-1,4) # Much faster
    verts = np.fromiter(itertools.chain.from_iterable(
        map(lambda line: line.split()[:3], lines[2:2+n_verts])), dtype=float).reshape(-1,3)
    faces = np.fromiter(itertools.chain.from_iterable(
        map(lambda line: line.split()[:3], lines[2+n_verts:2+n_verts+n_faces])), dtype=int).reshape(-1,3)
    return verts, faces


def write_asc(fname, verts, faces):
    with open(fname, 'w') as fout:
        fout.write('#!ascii version of surface mesh saved by mripy\n')
        np.savetxt(fout, [[len(verts), len(faces)]], fmt='%d')
        np.savetxt(fout, np.c_[verts, np.zeros(len(verts))], fmt=['%.6f', '%.6f', '%.6f', '%d'])
        np.savetxt(fout, np.c_[faces, np.zeros(len(faces))], fmt='%d')    


def read_label(fname):
    '''Read FreeSurfer label'''
    x = read_txt(fname)
    nodes = np.int_(x[:,0])
    coords = x[:,1:4]
    labels = x[:,4]
    return nodes, coords, labels


NIML_DSET_CORE_TAGS = ['INDEX_LIST', 'SPARSE_DATA']

def read_niml_dset(fname, tags=None, as_asc=True, return_type='list'):
    if tags is None:
        tags = NIML_DSET_CORE_TAGS
    if as_asc:
        temp_file = 'tmp.' + fname
        if not path.exists(temp_file):
            subprocess.check_call(['ConvertDset', '-o_niml_asc', '-input', fname, '-prefix', temp_file])
        root = etree.parse(temp_file).getroot()
        os.remove(temp_file)
        def get_data(tag):
            element = root.find(tag)
            return np.fromiter(element.text.split(), dtype=element.get('ni_type'))
        data = {tag: get_data(tag) for tag in tags}
    if return_type == 'list':
        return [data[tag] for tag in tags]
    elif return_type == 'dict':
        return data
    elif return_type == 'tree':
        return root


def read_niml_bin_nodes(fname):
    '''
    Read "Node Bucket" (node indices and values) from niml (binary) dataset.
    This implementation is experimental for one-column dset only.
    '''
    with open(fname, 'rb') as fin:
        s = fin.read()
        data = []
        for tag in NIML_DSET_CORE_TAGS:
            pattern = '<{0}(.*?)>(.*?)</{0}>'.format(tag)
            match = re.search(bytes(pattern, encoding='utf-8'), s, re.DOTALL)
            if match is not None:
                # attrs = match.group(1).decode('utf-8').split()
                # attrs = {k: v[1:-1] for k, v in (attr.split('=') for attr in attrs)}
                attrs = shlex.split(match.group(1).decode('utf-8')) # Don't split quoted string
                attrs = dict(attr.split('=') for attr in attrs)
                x = np.frombuffer(match.group(2), dtype=attrs['ni_type']+'32')
                data.append(x.reshape(np.int_(attrs['ni_dimen'])))
            else:
                data.append(None)
        if data[0] is None: # Non-sparse dataset
            data[0] = np.arange(data[1].shape[0])
        return data[0], data[1]


def write_niml_bin_nodes(fname, idx, val):
    '''
    Write "Node Bucket" (node indices and values) as niml (binary) dataset.
    This implementation is experimental for one-column dset only.

    References
    ----------
    [1] https://afni.nimh.nih.gov/afni/community/board/read.php?1,60396,60399#msg-60399
    [2] After some trial-and-error, the following components are required:
        self_idcode, COLMS_RANGE, COLMS_TYPE (tell suma how to interpret val), 
        no whitespace between opening tag and binary data.
    '''
    with open(fname, 'wb') as fout:
        # AFNI_dataset
        fout.write('<AFNI_dataset dset_type="Node_Bucket" self_idcode="{0}" \
            ni_form="ni_group">\n'.format(generate_afni_idcode()).encode('utf-8'))
        # COLMS_RANGE
        fout.write('<AFNI_atr ni_type="String" ni_dimen="1" atr_name="COLMS_RANGE">\
            "{0} {1} {2} {3}"</AFNI_atr>\n'.format(np.min(val), np.max(val), 
            idx[np.argmin(val)], idx[np.argmax(val)]).encode('utf-8'))
        # COLMS_TYPE
        col_types = {'int': 'Node_Index_Label', 'float': 'Generic_Float'}
        fout.write('<AFNI_atr ni_type="String" ni_dimen="1" atr_name="COLMS_TYPE">\
            "{0}"</AFNI_atr>\n'.format(col_types[get_ni_type(val)]).encode('utf-8'))
        # INDEX_LIST
        # Important: There should not be any \n after the opening tag for the binary data!
        fout.write('<INDEX_LIST ni_form="binary.lsbfirst" ni_type="int" ni_dimen="{0}" \
            data_type="Node_Bucket_node_indices">'.format(len(idx)).encode('utf-8'))
        fout.write(idx.astype('int32').tobytes())
        fout.write(b'</INDEX_LIST>\n')
        # SPARSE_DATA
        fout.write('<SPARSE_DATA ni_form="binary.lsbfirst" ni_type="{0}" ni_dimen="{1}" \
            data_type="Node_Bucket_data">'.format(get_ni_type(val), len(val)).encode('utf-8'))
        fout.write(val.astype(get_ni_type(val)+'32').tobytes())
        fout.write(b'</SPARSE_DATA>\n')
        fout.write(b'</AFNI_dataset>\n')


def generate_afni_idcode():
    return 'AFN_' + ''.join(random.choice(string.ascii_letters + string.digits) for n in range(22))


def get_ni_type(x):
    if np.issubdtype(x.dtype, np.integer):
        return 'int'
    elif np.issubdtype(x.dtype, np.floating):
        return 'float'


def write_1D_nodes(fname, idx, val):
    if idx is None:
        idx = np.arange(len(d))
    formats = dict(int='%d', float='%.6f')
    np.savetxt(fname, np.c_[idx, val], fmt=['%d', formats[get_ni_type(val)]])
    

class MaskDumper(object):
    def __init__(self, mask_file):
        self.mask_file = mask_file
        self.temp_file = 'tmp.dump.txt'
        subprocess.check_call(['3dmaskdump', '-mask', self.mask_file, '-index', '-xyz',
            '-o', self.temp_file, self.mask_file])
        x = np.loadtxt(self.temp_file)
        self.index = x[:,0].astype(int)
        self.ijk = x[:,1:4].astype(int)
        self.xyz = x[:,4:7]
        self.mask = x[:,7].astype(int)
        os.remove(self.temp_file)

    def dump(self, fname):
        files = glob.glob(fname) if isinstance(fname, six.string_types) else fname
        subprocess.check_call(['3dmaskdump', '-mask', self.mask_file, '-noijk',
            '-o', self.temp_file, ' '.join(files)])
        x = np.loadtxt(self.temp_file)
        os.remove(self.temp_file)
        return x

    def undump(self, prefix, x):
        np.savetxt(self.temp_file, np.c_[self.ijk, x])
        subprocess.check_call(['3dUndump', '-master', self.mask_file, '-ijk',
            '-prefix', prefix, '-overwrite', self.temp_file])
        os.remove(self.temp_file)


class Mask(object):
    def __init__(self, master=None, kind='mask'):
        self.master = master
        self.value = None
        if self.master is not None:
            self._infer_geometry(self.master)
            self.value = read_afni(self.master).ravel('F')
            if kind == 'mask':
                idx = self.value > 0 # afni uses Fortran index here
                self.value = self.value[idx]
                self.index = np.arange(np.prod(self.IJK))[idx]
            elif kind == 'full':
                self.index = np.arange(np.prod(self.IJK))

    def _infer_geometry(self, fname):
        self.IJK = afni.get_dims(fname)[:3]
        # res = afni.check_output(['cat_matvec', self.master+'::IJK_TO_DICOM', '-ONELINE'])[-2] # IJK_TO_DICOM_REAL??
        # self.MAT = np.fromiter(map(float, res.split()), float).reshape(3,4)
        res = afni.check_output(['3dAttribute', 'ORIGIN', fname])[-2]
        ORIGIN = np.fromiter(map(float, res.split()), float)
        res = afni.check_output(['3dAttribute', 'DELTA', fname])[-2]
        DELTA = np.fromiter(map(float, res.split()), float)
        self.MAT = np.c_[np.diag(DELTA), ORIGIN][[0,2,1],:]

    @classmethod
    def from_expr(cls, expr=None, **kwargs):
        master = list(kwargs.values())[0]
        mask = cls(master=None)
        mask.master = master
        mask._infer_geometry(master)
        data = {v: read_afni(f).squeeze() for v, f in kwargs.items()}
        idx = eval(expr, data).ravel('F') > 0
        mask.index = np.arange(np.prod(mask.IJK))[idx]
        return mask

    def compatible(self, other):
        return np.all(self.IJK==other.IJK) and np.allclose(self.MAT, other.MAT)

    def __repr__(self):
        return 'Mask ({0} voxels)'.format(len(self.index))

    def __add__(self, other):
        '''Mask union. Both masks are assumed to share the same grid.'''
        assert(self.compatible(other))
        mask = copy.deepcopy(self)
        mask.index = np.union1d(self.index, other.index)
        return mask

    def __mul__(self, other):
        '''Mask intersection. Both masks are assumed to share the same grid.'''
        assert(self.compatible(other))
        mask = copy.deepcopy(self)
        mask.index = np.intersect1d(self.index, other.index)
        return mask

    def __sub__(self, other):
        '''
        Voxels that are in the 1st mask but not in the 2nd mask.
        Both masks are assumed to share the same grid.
        '''
        assert(self.compatible(other))
        mask = copy.deepcopy(self)
        mask.index = mask.index[~np.in1d(self.index, other.index, assume_unique=True)]
        return mask

    def __contains__(self, other):
        assert(self.compatible(other))
        return np.all(np.in1d(other.index, self.index, assume_unique=True))

    def constrain(self, func, return_selector=False, inplace=False):
        '''
        Parameters
        ----------
        func : callable
            selector = func(x, y, z) is used to select a subset of self.index
        '''
        ijk1 = np.c_[np.unravel_index(self.index, self.IJK, order='F') + (np.ones_like(self.index),)]
        xyz = np.dot(self.MAT, ijk1.T).T # Yes, it is xyz here!
        selector = func(xyz[:,0], xyz[:,1], xyz[:,2])
        mask = self if inplace else copy.deepcopy(self)
        mask.index = mask.index[selector]
        return mask if not return_selector else (mask, selector)

    def infer_selector(self, smaller):
        assert(smaller in self)
        selector = np.in1d(self.index, smaller.index, assume_unique=True)
        return selector

    def near(self, x, y, z, r, **kwargs):
        '''mm'''
        if np.isscalar(r):
            r = np.ones(3) * r
        func = (lambda X, Y, Z: ((X-x)/r[0])**2 + ((Y-y)/r[1])**2 + ((Z-z)/r[2])**2 < 1)
        return self.constrain(func, **kwargs)

    def ball(self, c, r, **kwargs):
        # return self.near(*c, r, **kwargs) # For python 2.7 compatibility
        return self.near(c[0], c[1], c[2], r, **kwargs)

    def cylinder(self, c, r, **kwargs):
        '''The elongated axis is represented as nan'''
        if np.isscalar(r):
            r = np.ones(3) * r
        func = (lambda X, Y, Z: np.nansum(np.c_[((X-c[0])/r[0])**2, ((Y-c[1])/r[1])**2, ((Z-c[2])/r[2])**2], axis=1) < 1)
        return self.constrain(func, **kwargs)

    def slab(self, x1=None, x2=None, y1=None, y2=None, z1=None, z2=None, **kwargs):
        limits = np.dot(self.MAT, np.c_[np.r_[0,0,0,1], np.r_[self.IJK-1,1]])
        x1 = np.min(limits[0,:]) if x1 is None else x1
        x2 = np.max(limits[0,:]) if x2 is None else x2
        y1 = np.min(limits[1,:]) if y1 is None else y1
        y2 = np.max(limits[1,:]) if y2 is None else y2
        z1 = np.min(limits[2,:]) if z1 is None else z1
        z2 = np.max(limits[2,:]) if z2 is None else z2
        func = (lambda X, Y, Z: (x1<X)&(X<x2) & (y1<Y)&(Y<y2) & (z1<Z)&(Z<z2))
        return self.constrain(func, **kwargs)

    def dump(self, fname):
        files = glob.glob(fname) if isinstance(fname, six.string_types) else fname
        # return np.vstack(read_afni(f).T.flat[self.index] for f in files).T.squeeze() # Cannot handle 4D...
        data = []
        for f in files:
            vol = read_afni(f)
            S = vol.shape
            T = list(range(vol.ndim))
            T[:3] = T[:3][::-1]
            data.append(vol.transpose(*T).reshape(np.prod(S[:3]),int(np.prod(S[3:])))[self.index,:])
        return np.hstack(data).squeeze()

    def undump(self, prefix, x, method='nibabel'):
        if method == 'nibabel': # Much faster
            temp_file = 'tmp.%s.nii' % next(tempfile._get_candidate_names())
            vol = np.zeros(self.IJK) # Don't support int64?？
            assert(self.index.size==x.size)
            vol.T.flat[self.index] = x
            mat = np.dot(np.diag([-1,-1, 1]), self.MAT) # Have to do this to get RSA (otherwise it's LSP), don't know why... (PS. ijk -> xzy)
            aff = nibabel.affines.from_matvec(mat[:,:3], mat[:,3])
            img = nibabel.Nifti1Image(vol, aff)
            nibabel.save(img, temp_file)
            subprocess.check_call(['3dcopy', temp_file, prefix+'+orig', '-overwrite']) # However, still TLRC inside...
            os.remove(temp_file)
        elif method == '3dUndump': # More robust
            temp_file = 'tmp.%s.txt' % next(tempfile._get_candidate_names())
            ijk = np.c_[np.unravel_index(self.index, self.IJK, order='F')]
            np.savetxt(temp_file, np.c_[ijk, x])
            subprocess.check_call(['3dUndump', '-master', self.master, '-ijk',
                '-prefix', prefix, '-overwrite', temp_file])
            os.remove(temp_file)

    @property
    def ijk(self):
        return np.c_[np.unravel_index(self.index, self.IJK, order='F')]

    @property
    def xyz(self):
        return np.dot(self.MAT[:,:3], self.ijk.T).T + self.MAT[:,3]


class BallMask(Mask):
    def __init__(self, master, c, r):
        Mask.__init__(self, master, kind='full')
        self.ball(c, r, inplace=True)


class CylinderMask(Mask):
    def __init__(self, master, c, r):
        Mask.__init__(self, master, kind='full')
        self.cylinder(c, r, inplace=True)


class SlabMask(Mask):
    def __init__(self, master, x1=None, x2=None, y1=None, y2=None, z1=None, z2=None):
        Mask.__init__(self, master, kind='full')
        self.slab(x1, x2, y1, y2, z1, z2, inplace=True)


if __name__ == '__main__':
    pass