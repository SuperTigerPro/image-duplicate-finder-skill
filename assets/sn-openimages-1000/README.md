# Bundled Open Images SN background

This directory contains the default score-normalization (SN) background index for the Skill. It was built from an independently sampled subset of **1,000 Open Images validation images**, using sampling seed **20261127**, for the Copydays evaluation. It is a Skill-prepared background, not a background package released or endorsed by the CopyNCE authors. It does not reproduce the official DISC21 million-image auxiliary-set setup.

## Contents and transformation

- [features.npz](features.npz): normalized, whole-image **512-dimensional FP32 descriptors** extracted with the official CopyNCE Descriptor ViT-S/224, together with content hashes used to exclude identical inputs. No source photographs, image paths, login details or runtime scan reports are included.
- [manifest.json](manifest.json): model and preprocessing identity, provenance and integrity fingerprints.
- [attribution.csv](attribution.csv): one row per background image, preserving the author, supplied title, source page, author profile and license URL from the published Open Images metadata. The additional hashes identify the image and its canonical RGB pixels.

The source photographs were decoded, EXIF-oriented, converted to RGB and processed into numeric descriptors. The photographs themselves are not redistributed in this directory. Runtime uses the index through a private cache outside Git worktrees; normal scanning does not upload the selected images.

## Source credits and license

Source metadata: [Open Images validation image metadata](https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv). The published metadata lists all 1,000 selected photographs under **[Creative Commons Attribution 2.0 Generic](https://creativecommons.org/licenses/by/2.0/)**. Individual creators, supplied titles and source links are recorded in [attribution.csv](attribution.csv); retain this attribution file and these notices when redistributing this asset.

The [Open Images license notice](https://storage.googleapis.com/openimages/web/factsfigures.html#licenses) states that image licenses are listed from the source data and are not warranted by Open Images. This package preserves those notices; it does not claim independent confirmation from each photographer. See the [CC BY 2.0 legal code](https://creativecommons.org/licenses/by/2.0/legalcode.en) for the source-license terms.

The repository's MIT license covers the Skill's original code; it does not replace or remove the source creators' rights or attribution requirements for this asset. This use implies no endorsement by the photographers, Open Images, Google, or the CopyNCE authors.

## Scope

The index uses official query-side SN with `alpha=1`, `start=1`, `end=5`. It was checked against the evaluated Copydays files for identical source bytes and canonical RGB pixels, but this does not prove absence of every near-duplicate or shared source across datasets. New scans still exclude matching background identities. The 1,000-image background is a convenient default, not a claim of optimal calibration for every image domain; an explicitly supplied independent background index can override it.
