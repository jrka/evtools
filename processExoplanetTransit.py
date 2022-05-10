import argparse
from genericpath import isfile
import os
import pathlib
from tkinter import E
from astropy.io import fits
import cv2
import csv
import numpy as np
import subprocess

def runsolving(ra, dec, infile, outfile):
    try:
        rslt = subprocess.run(["solve-field", infile,
            "--no-plots", "--overwrite",
            "--ra", str(ra),
            "--dec", str(dec),
            "--radius", "5",
            "--new-fits", outfile ], 
            timeout=10, capture_output=True)
        if rslt.returncode != 0:
            print("Error solving %s - skipping" % f)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("Timeout solving %s - skipping" % f)
        return False

def runstacking(ra, dec, fitsfiles, stackfile, mjdobs, mjdend): 
    print("Stacking %d images into %s" % (len(fitsfiles), stackfile))
    tmpst = os.path.join(tmppath, "tmpstack.fits")
    stackargs = [ "SWarp", 
        "-IMAGEOUT_NAME", tmpst, 
        "-WRITE_XML", "N",
        "-CENTER_TYPE", "MANUAL",
        "-CENTER", "%f,%f" % (ra, dec),
        "-RESAMPLE_DIR", tmppath,
        "-COPY_KEYWORDS", "OBJECT,ORIGIN,MINSYET,TELESCOP,INSTUME,SERIALNB,TIMEUNIT,LATITUDE,LONGITUD,GAIN,GAINDB,ALTITUDE,CMOSTEMP,OBSMODE,DATE,SOFTVER" ]                              
    stackargs.extend(fitsfiles)
    rslt = subprocess.run(stackargs, capture_output=True)
    if rslt.returncode != 0:
        print("Error stacking %s" % stackfile)
        return False
    # And resolve new file
    if runsolving(ra, dec, tmpst, stackfile):
        # And add MJD fields for stack
        with fits.open(stackfile) as hduList:
            hduList[0].header['MJD-OBS'] = mjdobs
            hduList[0].header['MJD-END'] = mjdend
            hduList[0].header['MJD-MID'] = (mjdobs + mjdend) / 2
            hduList.writeto(stackfile, overwrite=True)
        return True
    return False
# Initialize parser
parser = argparse.ArgumentParser()
# Add input argument
parser.add_argument("csvfile", help = "Input CSV file") 
# Adding output argument
parser.add_argument("-o", "--output", help = "Output directory") 
# Add flags (default is grey - others for monochrome)
parser.add_argument('-r', "--red", action='store_true')
parser.add_argument('-g', "--green", action='store_true')
parser.add_argument('-b', "--blue", action='store_true')
parser.add_argument("-G", "--gray", action='store_true')
# Add number of minutes to stack
parser.add_argument("--stacktime", help = "Number of mintutes to stack (default 2)") 
parser.add_argument("--presolve", action='store_true')

# Read arguments from command line
try:
    args = parser.parse_args()
except argparse.ArgumentError:
    os.exit(1)
outputdir='.'
if args.output: 
    outputdir = args.output
stacktime = 2
if args.stacktime:
    stacktime = int(args.stacktime)
if args.csvfile is None:
    print("CSV filename is required")
    os.exit(1)
if os.path.isfile(args.csvfile) == False:
    print("CSV file not found: ", args.csvfile)
    os.exit(1)
basedir = os.path.dirname(args.csvfile)

# Make output directory, if needed
pathlib.Path(outputdir).mkdir(parents=True, exist_ok=True)
darkpath = os.path.join(outputdir, "darks")
pathlib.Path(darkpath).mkdir(parents=True, exist_ok=True)
sciencepath = os.path.join(outputdir, "science")
pathlib.Path(sciencepath).mkdir(parents=True, exist_ok=True)
tmppath = os.path.join(outputdir, "tmp")
pathlib.Path(tmppath).mkdir(parents=True, exist_ok=True)
stackedpath = os.path.join(outputdir, "stacked")
pathlib.Path(stackedpath).mkdir(parents=True, exist_ok=True)

togray = False
coloridx = 0
if args.red:
    coloridx = 2   # Red
    print("Produce red channel FITS files")
elif args.green:
    coloridx = 1   # Green
    print("Produce green channel FITS files")
elif args.blue:
    coloridx = 0   # Blue
    print("Produce blue channel FITS files")
else:
    togray = True
    print("Produce grayscale FITS files")
dopresolve = False
if args.presolve:
    dopresolve = True
darkfiles = []
lightfiles = []
with open(args.csvfile, newline='') as csvfile:
    csvreader = csv.DictReader(csvfile)
    for row in csvreader:
        category = row['category']
        if category == 'dark':
            darkfiles.append(row['filename'])
        elif category == 'science':
            lightfiles.append(row['filename']) 
    print("CSV file has %d darks and %d science FITS files" % (len(darkfiles), len(lightfiles)))

dark = fits.HDUList()

# Build dark frame, if we have any to work with
if len(darkfiles) > 0:
    for f in darkfiles:
        try:
            fname = os.path.join(basedir, f)
            # Load file into list of HDU list 
            with fits.open(fname) as hduList:
                # Use first one as base
                if len(dark) == 0:
                    darkaccum = np.zeros(hduList[0].data.shape)
                    dark.append(hduList[0].copy())
                np.add(darkaccum, hduList[0].data, out=darkaccum)
                hduList.writeto(os.path.join(darkpath, f), overwrite=True)
        except OSError:
            print("Error: file %s" % f)        
    # Now compute average for each pixel
    darkaccum = darkaccum // len(darkfiles)
    dark[0].data = darkaccum.astype(np.uint16)
    # And write output dark
    dark.writeto(os.path.join(darkpath, "master-dark.fits"), overwrite=True)

cnt = 0
solvedcnt = 0
timeaccumlist = []
timeaccumstart = 0
timeaccumra = 0
timeaccumdec = 0
mjdobs = 0
mjdend = 0
stackedcnt = 0
for f in lightfiles:
    try:
        # Load file into list of HDU list 
        fname = os.path.join(basedir, f)
        with fits.open(fname) as hduList:
            print("Processing %s" % fname)
            # First, calibrate image
            if len(dark) > 0:
                # Clamp the data with the dark from below, so we can subtract without rollover
                np.maximum(hduList[0].data, dark[0].data, out=hduList[0].data)
                # And subtract the dark
                np.subtract(hduList[0].data, dark[0].data, out=hduList[0].data)
            # Now debayer into grayscale                
            if togray:
                dst = cv2.cvtColor(hduList[0].data, cv2.COLOR_BayerRG2GRAY)
                for idx, val in enumerate(dst):
                    hduList[0].data[idx] = val
            else:
                # Demosaic the image
                dst = cv2.cvtColor(hduList[0].data, cv2.COLOR_BayerRG2BGR)
                for idx, val in enumerate(dst):
                    hduList[0].data[idx] = val[:,coloridx]
            rslt = True
            newfits = os.path.join(sciencepath, f)
            if dopresolve:
                # Write to temporary file so that we can run solve-field to
                # set WCS data
                hduList.writeto(os.path.join(tmppath, "tmp.fits"), overwrite=True)
                # Now run solve-field to generate final file
                rslt = runsolving(hduList[0].header['FOVRA'], hduList[0].header['FOVDEC'],
                    os.path.join(tmppath, "tmp.fits"), newfits )
            else:   # No presolve
                hduList.writeto(newfits, overwrite=True)
            if rslt == False:
                print("Error solving %s - skipping" % f)
            else:
                tobs = hduList[0].header['MJD-OBS'] * 24 * 60 # MJD in minutes
                # End of accumulator?
                if (len(timeaccumlist) > 0) and ((timeaccumstart + stacktime) < tobs):
                    stackout = os.path.join(stackedpath, "stack-%04d.fits" % stackedcnt)
                    tmpout = os.path.join(tmppath, "tmpstack.fits")
                    try: 
                        runstacking(timeaccumra, timeaccumdec, timeaccumlist, stackout, mjdobs, mjdend)
                        stackedcnt = stackedcnt + 1
                    except OSError as e:
                        print("Error: stacking file %s - %s (%s)" % (stackout, e.__class__, e))     
                    timeaccumlist = []
                    timeaccumstart = 0
                else:
                    # If first one to accumulate, save start time and RA/Dec
                    if (len(timeaccumlist) == 0):
                        timeaccumstart = tobs
                        timeaccumra = hduList[0].header['FOVRA']
                        timeaccumdec = hduList[0].header['FOVDEC']
                        mjdobs = hduList[0].header['MJD-OBS']
                    timeaccumlist.append(newfits)
                    mjdend = hduList[0].header['MJD-END']
                solvedcnt = solvedcnt + 1
            cnt = cnt + 1
    except OSError as e:
        print("Error: file %s - %s (%s)" % (f, e.__class__, e))     
# Final accumulator?
if len(timeaccumlist) > 0:
    stackout = os.path.join(stackedpath, "stack-%04d.fits" % stackedcnt)
    try: 
        runstacking(timeaccumra, timeaccumdec, timeaccumlist, stackout, mjdobs, mjdend)
        stackedcnt = stackedcnt + 1
    except OSError as e:
        print("Error: stacking file %s - %s (%s)" % (stackout, e.__class__, e))     

print("Processed %d out of %d files and %d stacks into destination '%s'" % (solvedcnt, cnt, stackedcnt, outputdir))

