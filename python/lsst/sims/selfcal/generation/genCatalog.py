import numpy as np
import lsst.sims.maf.db as db
from lsst.sims.selfcal.generation.starsTools import starsProject, assignPatches
import numpy.lib.recfunctions as rfn
from .offsets import OffsetSNR


def wrapRA(ra):
    """Wraps RA into 0-360 degrees."""
    ra = ra % 360.0
    return ra

def capDec(dec):
    """Terminates declination at +/- 90 degrees."""
    dec = np.where(dec>90, 90, dec)
    dec = np.where(dec<-90, -90, dec)
    return dec


def genCatalog(visits, starsDbAddress, offsets=None, lsstFilter='r', raBlockSize=20., decBlockSize=10.,
               nPatches=16, radiusFoV=1.8, verbose=True, seed=42, obsFile='observations.dat', truthFile='starInfo.dat'):
    """
    Generate a catalog of observed stellar magnitudes.

    visits:  A numpy array with the properties of the visits.  Expected to have Opsim-like values
    starsDbAddress:  a sqlAlchemy address pointing to a database that contains properties of stars used as input.
    offsets:  A list of instatiated classes that will apply offsets to the stars
    lsstFilter:  Which filter to use for the observed stars
    obsFile:  File to write the observed stellar magnitudes to
    truthFile:  File to write the true stellar magnitudes to
    raBlockSize/decBlockSize:  Size of chucks to use when looping over the sky
    nPatches:  Number of patches to divide the FoV into.  Must be an integer squared
    radiusFoV: Radius of the telescope field of view in degrees
    seed: random number seed
    """

    if offsets is None:
        # Maybe change this to just run with a default SNR offset
        warnings.warn("Warning, no offsets set, returning without running")
        return

    # Open files for writing
    ofile = open(obsFile, 'w')
    tfile = open(truthFile, 'w')

    print >>ofile, '#PatchID, StarID, ObservedMag, MagUncertainty, Radius, HPID'
    print >>tfile, '#StarID, TrueMag'

    # Set up connection to stars db:
    msrgbDB = db.Database(starsDbAddress, dbTables={'stars':['stars', 'id']})
    starCols = ['id', 'rmag', 'gmag', 'ra', 'decl']
    if lsstFilter+'mag' not in starCols:
        starCols.append(lsstFilter+'mag' )

    # Loop over the sky
    raBlocks = np.arange(0.,2.*np.pi, np.radians(raBlockSize))
    decBlocks = np.arange(-np.pi, np.pi, np.radians(decBlockSize) )

    # List to keep track of which starIDs have been written to truth file
    idsUsed = []
    nObs = 0

    magUncert = OffsetSNR(lsstFilter=lsstFilter)

    for raBlock in raBlocks:
        for decBlock in decBlocks:
            # The idea here is that this could be run in parallel or not and is still repeatable
            # b/c the seed will always be the same for each block.
            np.random.seed(seed)
            seed += 1 #This should make it possible to run in parallel and maintain repeatability.
            visitsIn = visits[np.where( (visits['ra'] >=
                      raBlock) & (visits['ra'] < raBlock+np.radians(raBlockSize)) &
                      (visits['dec'] >=  decBlock) &
                      (visits['dec'] < decBlock+np.radians(decBlockSize)) )]
            if np.size(visitsIn) > 0:
                # Fetch the stars in this block+the radiusFoV
                decPad = radiusFoV
                raPad = radiusFoV
                # Need to deal with wrap around effects.
                decMin = capDec(np.degrees(decBlock)-decPad)
                decMax = capDec(np.degrees(decBlock)+decBlockSize+decPad)
                sqlwhere = 'decl > %f and decl < %f '%(decMin, decMax)
                raMin = np.degrees(raBlock)-raPad/np.cos(np.radians(decMin))
                raMax = np.degrees(raBlock)+raBlockSize+raPad/np.cos(np.radians(decMin))

                if (wrapRA(raMin) != raMin) & (wrapRA(raMax) != raMax):
                    # near a pole, just grab all the stars
                    sqlwhere += ''
                else:
                    raMin = wrapRA(raMin)
                    raMax = wrapRA(raMax)
                    # no wrap
                    if raMin < raMax:
                        sqlwhere += 'and ra < %f and ra > %f '%(raMax,raMin)
                    # One side wrapped
                    else:
                        sqlwhere += 'and (ra > %f or ra < %f)'%(raMin,raMax)
                print 'quering stars with: '+sqlwhere
                stars = msrgbDB.tables['stars'].query_columns_Array(
                    colnames=starCols, constraint=sqlwhere)
                print 'got %i stars'%stars.size
                # Add all the columns I will want here, then I only do
                # one numpy stack per block rather than lots of stacks per visit
                # Ugh, feels like writing fortran though...
                newcols = ['x', 'y', 'radius', 'patchID', 'subPatch', 'hpID', 'hp1', 'hp2', 'hp3', 'hp4']
                newtypes = [float, float, float, int,int, int]
                stars = rfn.merge_arrays([stars, np.zeros(stars.size, dtype=zip(newcols,newtypes))],
                                         flatten=True, usemask=False)

            for visit in visitsIn:
                dmags = {}
                # Calc x,y, radius for each star, crop off stars outside the FoV
                # XXX - plan to replace with code to see where each star falls and get chipID.
                starsIn = starsProject(stars, visit)
                starsIn = starsIn[np.where(starsIn['radius'] <= np.radians(radiusFoV))]

                newIDs = np.in1d(starsIn['id'], idsUsed, invert=True)
                idsUsed.extend(starsIn['id'][newIDs].tolist())

                # Assign patchIDs and healpix IDs
                starsIn = assignPatches(starsIn, visit, nPatches=nPatches)
                #maybe make dmags a dict on stars, so that it's faster to append them all?

                # Apply the offsets that have been configured
                for offset in offsets:
                    dmags[offset.newkey] = offset.run(starsIn, visit, dmags=dmags)

                # Total up all the dmag's to make the observed magnitude
                keys = dmags.keys()
                obsMag = starsIn['%smag'%lsstFilter]
                for key in keys:
                    obsMag += dmags[key]
                nObs += starsIn.size

                # Calculate the uncertainty in the observed mag:
                magErr = magUncert.calcMagErrors(obsMag, errOnly=True, m5=visit['fiveSigmaDepth'])

                # patchID, starID, observed Mag, mag uncertainty, radius, healpixIDs
                for star,obsmag,magE in zip(starsIn,obsMag,magErr):
                    print >>ofile, "%i, %i, %f, %f, %f, %i "%( \
                        star['patchID'],star['id'], obsmag, magE,
                        star['radius'], 0) #star['hpID'])

                # Note the new starID's and print those to a truth file
                # starID true mag
                # XXX--might be better to just collect these and print at the end so that they can be sorted?
                # XXX - even better, just save them to a numpy save file, sorted, since the obs are teh only
                # thing that really needs to go to ASCII!
                for ID,mag in zip(starsIn['id'][newIDs],starsIn['%smag'%lsstFilter][newIDs]):
                    print >>tfile, '%i, %f'%(ID, mag)

                # Calc and print a patch file.  Note this is slightly ambiguous since the clouds can have structure
                # patchID magOffset

                # Look at the distribution of dmags
                # patchID, starID, dmag1, dmag2, dmag3...


    ofile.close()
    tfile.close()
    return nObs, len(idsUsed)
