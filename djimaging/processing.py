import datajoint as dj
import scanreader
import numpy as np
import pathlib
from datetime import datetime

from .parameter import CaimanParamSet, Suite2pParamSet
from .imaging import schema, Scan, ScanInfo, Channel, PhysicalFile
from djutils.templates import required, optional

# ===================================== Lookup =====================================


@schema
class ProcessingMethod(dj.Lookup):
    definition = """
    processing_method: char(8)
    """

    contents = zip(['suite2p', 'caiman'])


@schema
class ProcessingParamSet(dj.Lookup):
    definition = """
    -> ProcessingMethod
    paramset_idx:  smallint
    ---
    paramset_desc: varchar(128)
    """

    class Caiman(dj.Part):
        definition = """
        -> master
        ---
        -> CaimanParamSet
        """

    class Suite2p(dj.Part):
        definition = """
        -> master
        ---
        -> Suite2pParamSet
        """


@schema
class CellCompartment(dj.Lookup):
    definition = """  # cell compartments that can be imaged
    cell_compartment         : char(16)
    """
    contents = [['axon'], ['soma'], ['bouton']]


@schema
class RoiType(dj.Lookup):
    definition = """ # possible classifications for a segmented mask
    roi_type        : varchar(16)
    """
    contents = [
        ['soma'],
        ['axon'],
        ['dendrite'],
        ['neuropil'],
        ['artifact'],
        ['unknown']
    ]


# ===================================== Trigger a processing routine =====================================

@schema
class ProcessingTask(dj.Manual):
    definition = """
    -> Scan
    processing_instance: uuid
    ---
    -> ProcessingParamSet
    """


@schema
class Processing(dj.Computed):
    definition = """
    -> ProcessingTask
    ---
    processing_time: datetime  # time of generation of this set of processed, segmented results
    """

    class ProcessingOutputFile(dj.Part):
        definition = """
        -> master
        -> PhysicalFile
        """

    @staticmethod
    @optional
    def _get_caiman_dir(processing_task_key: dict) -> str:
        """
        Retrieve the CaImAn output directory for a given ProcessingTask
        :param processing_task_key: a dictionary of one ProcessingTask
        :return: a string for full path to the resulting CaImAn output directory
        """
        return None

    @staticmethod
    @optional
    def _get_suite2p_dir(processing_task_key: dict) -> str:
        """
        Retrieve the Suite2p output directory for a given ProcessingTask
        :param processing_task_key: a dictionary of one ProcessingTask
        :return: a string for full path to the resulting CaImAn output directory
        """
        return None

    def make(self, key):
        # ----
        # trigger suite2p or caiman here
        # ----

        method = (ProcessingMethod & key).fetch1('processing_method')

        if method == 'suite2p':
            data_dir = pathlib.Path(self._get_suite2p_dir(key))
        elif method == 'caiman':
            data_dir = pathlib.Path(self._get_caiman_dir(key))
        else:
            raise NotImplementedError(f'Unknown method: {method}')

        if data_dir.exist():
            key = {**key, 'processing_time': datetime.now()}
            self.insert1(key)
            # Insert file(s)
            root = pathlib.Path(PhysicalFile._get_root_data_dir())
            files = data_dir.glob('*')  # maybe something more file-specific
            self.ScanFile.insert([{**key, 'file_path': pathlib.Path(f).relative_to(root).as_posix()}
                                  for f in files if f.is_file()])
        else:
            # output directory does not exist:
            # 1. trigger processing here (suite2p or caiman)
            # 2. make some indicator that the processing is ongoing (e.g. is_running.txt)
            # 3. exit out
            return


# ===================================== Motion Correction =====================================

@schema
class MotionCorrection(dj.Imported):
    definition = """ 
    -> ProcessingTask
    ---
    -> Channel.proj(mc_channel='channel')              # channel used for motion correction in this processing task
    """

    class RigidMotionCorrection(dj.Part):
        definition = """ 
        -> master
        -> ScanInfo.Field
        ---
        ref_image                       : longblob      # image used as alignment template
        outlier_frames                  : longblob      # mask with true for frames with outlier shifts (already corrected)
        y_shifts                        : longblob      # (pixels) y motion correction shifts
        x_shifts                        : longblob      # (pixels) x motion correction shifts
        y_std                           : float         # (pixels) standard deviation of y shifts
        x_std                           : float         # (pixels) standard deviation of x shifts
        """

    class NonRigidMotionCorrection(dj.Part):
        """ Piece-wise rigid motion correction - tile the FOV into multiple 2D blocks/patches"""
        definition = """ 
        -> master
        -> ScanInfo.Field
        ---
        ref_image                       : longblob      # image used as alignment template
        outlier_frames                  : longblob      # mask with true for frames with outlier shifts (already corrected)
        block_height                    : int           # (px)
        block_width                     : int           # (px)
        block_count_y                   : int           # number of blocks tiled in the y direction
        block_count_x                   : int           # number of blocks tiled in the x direction
        """

    class Block(dj.Part):
        definition = """
        -> master
        -> master.NonRigidMotionCorrection
        block_id                        : int
        ---
        block_y                         : longblob      # (y_start, y_end) in pixel of this block
        block_x                         : longblob      # (x_start, x_end) in pixel of this block
        y_shifts                        : longblob      # (pixels) y motion correction shifts for every frame
        x_shifts                        : longblob      # (pixels) x motion correction shifts for every frame
        y_std                           : float         # (pixels) standard deviation of y shifts
        x_std                           : float         # (pixels) standard deviation of x shifts
        """


@schema
class MotionCorrectedImages(dj.Imported):
    definition = """ # summary images for each field and channel after corrections
    -> MotionCorrection
    -> ScanInfo.Field
    -> Channel
    ---
    average_image                : longblob
    correlation_image=null       : longblob
    max_proj_image=null          : longblob
    """

# ===================================== Segmentation =====================================


@schema
class Segmentation(dj.Computed):
    definition = """ # Different mask segmentations.
    -> MotionCorrection        
    ---
    -> Channel.proj(seg_channel='channel')  # channel used for the segmentation
    """

    class Mask(dj.Part):
        definition = """ # A mask produced by segmentation.
        -> master
        mask                : smallint
        ---
        -> ScanInfo.Field                   # the field this ROI comes from
        npix = NULL         : int           # number of pixels in ROIs
        center_x            : int           # center x coordinate in pixels
        center_y            : int           # center y coordinate in pixels
        xpix                : longblob      # x coordinates in pixels
        ypix                : longblob      # y coordinates in pixels        
        weights             : longblob      # weights of the mask at the indices above in column major (Fortran) order
        """


@schema
class MaskClassification(dj.Computed):
    definition = """
    -> Segmentation
    """

    class MaskType(dj.Part):
        definition = """
        -> master
        -> Segmentation.Mask
        ---
        -> RoiType        
        """


# ===================================== Activity Trace =====================================


@schema
class Fluorescence(dj.Computed):
    definition = """  # fluorescence traces before spike extraction or filtering
    -> Segmentation
    """

    class Trace(dj.Part):
        definition = """
        -> master
        -> Segmentation.Mask
        -> Channel.proj(roi_channel='channel')  # the channel that this trace comes from 
        ---
        fluo                : longblob  # Raw fluorescence trace
        neuropil_fluo       : longblob  # Neuropil fluorescence trace
        """


@schema
class DeconvolvedCalciumActivity(dj.Computed):
    definition = """  # fluorescence traces before spike extraction or filtering
    -> Fluorescence
    """

    class DFF(dj.Part):
        definition = """  # delta F/F
        -> master
        -> Fluorescence.Trace
        ---
        df_f                : longblob  # delta F/F - deconvolved calcium acitivity 
        """
