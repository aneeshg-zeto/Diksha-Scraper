# DIKSHA Textbook PDF Extractor

Downloads chapter PDFs from all DIKSHA digital textbooks across every board, medium, and class.

---

## For teammates — extracting PDFs

### Step 1: Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

### Step 2: Copy the downloads folder

Get the `downloads/` folder from the shared hard drive / Google Drive and place it inside the cloned repo folder so the structure looks like:

```
YOUR_REPO/
  downloads/          ← copy this from the hard drive
  extract_pdfs.py
  requirements.txt
  ...
```

### Step 3: Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Run the extractor

```bash
python extract_pdfs.py
```

That's it. It will scan all the textbook packages in `downloads/`, find every chapter PDF, and download them automatically.

- Safe to stop and restart anytime — progress is saved in `pdf_manifest.json`
- Already downloaded PDFs are skipped on re-run
- No login or auth required

---

## Output structure

PDFs are saved inside the `downloads/` folder alongside the textbook packages:

```
downloads/
  CBSE/
    Class 6/
      Mathematics/
        (NEW) Mathematics.ecar
        (NEW) Mathematics/
          Chapter 1 - Knowing Our Numbers.pdf
          Chapter 2 - Whole Numbers.pdf
          ...
  NCERT/
    Class 9/
      Science/
        ...
  State (Karnataka)/
  State (Maharashtra)/
  ... (42 boards total)
```

---

## Requirements

- Python 3.12+ — download from https://python.org/downloads
- Internet connection (PDFs are downloaded from DIKSHA servers)
- ~16 GB free space for the `downloads/` folder (ecars already downloaded)
- Additional space for PDFs (estimated 50–100 GB depending on how many chapters have PDFs)
