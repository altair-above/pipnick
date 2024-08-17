
from pathlib import Path
import numpy as np
import logging
import warnings

from astropy.nddata import CCDData
from astropy.wcs.wcs import FITSFixedWarning
import astropy.units as u
import ccdproc

from pipnick.utils.nickel_data import (gain, read_noise, bias_label, 
                                       dome_flat_label, sky_flat_label,
                                       sky_flat_label_alt,
                                       dark_label, focus_label)
from pipnick.utils.nickel_masks import get_masks_from_file
from pipnick.utils.dir_nav import organize_files, norm_str

logger = logging.getLogger(__name__)


def reduce_all(maindir, table_path=None, save_inters=False,
               excl_files=[], excl_objs=[], excl_filts=[]):
    """
    Perform reduction of raw astronomical data frames (overscan subtraction,
    bias subtraction, flat division, cosmic ray masking).

    Parameters
    ----------
    maindir : str or Path
        Path to the parent directory of the raw directory
        containing the raw FITS files to be reduced.
    table_path : str or Path, optional
        Path to a pipnick-specific table file with information about
        which raw FITS files to process. Must be produced by [AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA]
    save_inters : bool, optional
        If True, save intermediate results during processing.
    excl_files : list, optional
        List of file stems to exclude (exact match not necessary).
    excl_objs : list, optional
        List of object strings to exclude (exact match not necessary).
    excl_filts : list, optional
        List of filter names to exclude.

    Returns
    -------
    list
        Paths to the saved reduced images.
    """
    logger.info(f"---- reduce_all() called on directory {maindir}")
    maindir = Path(maindir)
    rawdir = maindir / 'raw'
    
    # Organize raw files based on input directory or table
    file_df = organize_files(rawdir, table_path, 'reduction',
                             excl_files, excl_objs, excl_filts)
    
    reddir = maindir / 'reduced'
    procdir = maindir / 'processing'
    Path.mkdir(reddir, exist_ok=True)
    Path.mkdir(procdir, exist_ok=True)
    
    # Initialize CCDData objects and remove cosmic rays
    logger.info("Initializing CCDData objects & removing cosmic rays")
    warnings.simplefilter("ignore", category=FITSFixedWarning)
    ccd_objs = [init_ccddata(file) for file in file_df.files]
    file_df.files = ccd_objs
    
    # Generate master bias and master flats
    master_bias = get_master_bias(file_df, save=save_inters, save_dir=procdir)
    master_flats = get_master_flats(file_df, save=save_inters, save_dir=procdir)

    # Filter out non-science files
    scifiles_mask = ((file_df.objects != bias_label) &
                     (file_df.objects != dark_label) &
                     (file_df.objects != dome_flat_label) &
                     (file_df.objects != sky_flat_label) &
                     (file_df.objects != sky_flat_label_alt) &
                     (file_df.objects != focus_label)).values
    scifile_df = file_df.copy()[scifiles_mask]

    # Perform overscan subtraction & trimming
    logger.info(f"Performing overscan subtraction & trimming on {len(scifile_df.files)} science images")
    scifile_df.files = [trim_overscan(scifile) for scifile in scifile_df.files]
    if save_inters:
        save_results(scifile_df, 'over', procdir/'overscan')
    
    # Perform bias subtraction
    logger.info(f"Performing bias subtraction on {len(scifile_df.files)} science images")
    scifile_df.files = [ccdproc.subtract_bias(scifile, master_bias) 
                    for scifile in scifile_df.files]
    if save_inters:
        save_results(scifile_df, 'unbias', procdir/'unbias')

    # Perform flat division for each filter
    logger.info("Performing flat division")
    all_red_paths = []
    for filt in master_flats.keys():
        logger.debug(f"{filt} Filter:")
        scienceobjects = list(set(scifile_df.objects[scifile_df.filters == filt]))
        
        for scienceobject in scienceobjects:
            # Filter science files by object and filter
            sub_scifile_df = scifile_df.copy()[(scifile_df.objects == scienceobject) &
                                               (scifile_df.filters == filt)]
            # Create directory for each science target / filter combination
            sci_dir = reddir / (scienceobject + '_' + filt)
            
            # Perform flat division
            sub_scifile_df.files = [ccdproc.flat_correct(scifile, master_flats[filt]) 
                         for scifile in sub_scifile_df.files]
            
            red_paths = save_results(sub_scifile_df, 'red', sci_dir)
            all_red_paths += red_paths
    
    # Return
    logger.info(f"Fully reduced images saved to {reddir}")
    logger.info("---- reduce_all() call ended")
    return all_red_paths


def init_ccddata(frame):
    """
    Initialize a CCDData object from a FITS file and remove cosmic rays.

    Parameters
    ----------
    frame : str or Path
        Path to the FITS file.

    Returns
    -------
    CCDData
        Initialized and processed CCDData object.
    """
    logger.debug(f"Removing cosmic rays from {Path(frame).name}")
    ccd = CCDData.read(frame, unit=u.adu)
    ccd.mask = get_masks_from_file('fov_mask')
    ccd.mask[ccd.data > 62000] = True
    ccd = ccdproc.cosmicray_lacosmic(ccd, gain_apply=False, gain=gain, 
                                     readnoise=read_noise, verbose=False)
    # Apply gain manually due to a bug in cosmicray_lacosmic function
    ccd.data = ccd.data * gain
    # Bug in cosmicray_lacosmic returns CCDData.data as a Quanity with incorrect
    # units electron/ADU if gain_apply=True. Therefore, we manually apply gain,
    # and leave ccd.data as a numpy array
    return ccd

def trim_overscan(ccd):
    """
    Subtract overscan and trim the overscan region from the image.

    Parameters
    ----------
    ccd : CCDData
        CCDData object to process.

    Returns
    -------
    CCDData
        Processed CCDData object with overscan subtracted and image trimmed.
    """
    def nickel_oscansec(hdr):
        nc = hdr['NAXIS1']
        no = hdr['COVER']
        nr = hdr['NAXIS2']
        return f'[{nc-no+1}:{nc},1:{nr}]'
    
    oscansec = nickel_oscansec(ccd.header)
    proc_ccd = ccdproc.subtract_overscan(ccd, fits_section=oscansec, overscan_axis=1)
    return ccdproc.trim_image(proc_ccd, fits_section=ccd.header['DATASEC'])

def stack_frames(raw_frames, frame_type):
    """
    Stack frames by trimming overscan and combining them with sigma clipping.

    Parameters
    ----------
    raw_frames : list
        List of CCDData objects to combine.
    frame_type : str
        Type of frames (e.g., 'flat').

    Returns
    -------
    CCDData
        Combined CCDData object.
    """
    trimmed_frames = [trim_overscan(frame) for frame in raw_frames]
    combiner = ccdproc.Combiner(trimmed_frames)
    
    old_n_masked = 0
    new_n_masked = 1
    while new_n_masked > old_n_masked:
        combiner.sigma_clipping(low_thresh=3, high_thresh=3, func=np.ma.mean)
        old_n_masked = new_n_masked
        new_n_masked = combiner.data_arr.mask.sum()

    if frame_type == 'flat':
        scaling_func = lambda arr: 1/np.ma.average(arr)
        combiner.scaling = scaling_func
    stack = combiner.average_combine()  
    return stack

def get_master_bias(file_df, save=True, save_dir=None):
    """
    Create a master bias frame from individual bias frames.

    Parameters
    ----------
    file_df : pd.DataFrame
        DataFrame containing file information.
    save : bool, optional
        If True, save the master bias frame to disk.
    save_dir : Path or None, optional
        Directory to save the master bias frame.

    Returns
    -------
    CCDData
        Master bias CCDData object.
    """
    logger.info("Combining bias files into master bias")
    bias_df = file_df.copy()[file_df.objects == bias_label]
    logger.info(f"Using {len(bias_df.files)} bias frames: {[file.stem.split('_')[0] for file in bias_df.paths]}")

    master_bias = stack_frames(bias_df.files, frame_type='bias')
    
    if save:
        master_bias.header["OBJECT"] = "Master_Bias"
        master_bias.write(save_dir / 'master_bias.fits', overwrite=True)
        logger.info(f"Saving master bias to {save_dir / 'master_bias.fits'}")
    
    return master_bias

def get_master_flats(file_df, save=True, save_dir=None):
    """
    Create master flat frames (one per filter) from individual flat frames.

    Parameters
    ----------
    file_df : pd.DataFrame
        DataFrame containing file information.
    save : bool, optional
        If True, save the master flat frames to disk.
    save_dir : Path or None, optional
        Directory to save the master flat frames.

    Returns
    -------
    dict
        Dictionary of master flat CCDData objects keyed by filter.
    """
    logger.info("Combining flat files into master flat")
    
    # Use sky flats if available, else use dome flats
    if sky_flat_label in list(set(file_df.objects)):
        flattype = sky_flat_label
    elif sky_flat_label_alt in list(set(file_df.objects)):
        flattype = sky_flat_label_alt
    else:
        flattype = dome_flat_label
    logger.debug(f"Assuming that flat label names normalize to:  {sky_flat_label} or {sky_flat_label_alt} (sky flat) and {dome_flat_label} (dome flat)")
    logger.info(f"Using flat type '{flattype}'")
    
    master_flats = {}
    
    # Make a master flat for all filts in which flats have been taken
    for filt in set(file_df.filters[file_df.objects == flattype]):
        flat_df = file_df.copy()[(file_df.objects == flattype) & (file_df.filters == filt)]
        logger.info(f"Using {len(flat_df.files)} flat frames: {[path.stem.split('_')[0] for path in flat_df.paths]}")

        master_flat = stack_frames(flat_df.files, frame_type='flat')
        
        if save:
            master_flat.header["OBJECT"] = filt + "-Band_Master_Flat"
            master_flat.write(save_dir / ('master_flat_' + filt + '.fits'), overwrite=True)
            logger.info(f"Saving {filt}-band master flat to {save_dir / ('master_flat_' + filt + '.fits')}")
        master_flats[filt] = master_flat
    
    return master_flats

def save_results(scifile_df, modifier_str, save_dir):
    """
    Save (partially) processed science files to the specified directory.

    Parameters
    ----------
    scifile_df : pd.DataFrame
        DataFrame containing processed science file information.
    modifier_str : str
        String to append to filenames to indicate processing stage.
    save_dir : Path
        Directory to save the processed files.

    Returns
    -------
    list
        List of paths to the saved files.
    """
    Path.mkdir(save_dir, exist_ok=True)
    logger.info(f"Saving {len(scifile_df.files)} _{modifier_str} images {save_dir.name} images to {save_dir}")
    save_paths = [save_dir / (path.stem.split('_')[0] + f"_{modifier_str}" + path.suffix) for path in scifile_df.paths]
    for file, path in zip(scifile_df.files, save_paths):
        file.write(path, overwrite=True)
    return save_paths


bias_label = norm_str(bias_label)
dome_flat_label = norm_str(dome_flat_label)
sky_flat_label = norm_str(sky_flat_label)
sky_flat_label_alt = norm_str(sky_flat_label_alt)
dark_label = norm_str(dark_label)
focus_label = norm_str(focus_label)

