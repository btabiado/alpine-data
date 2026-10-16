# Drop the saved NUFORC page here

1. Open https://nuforc.org/subndx/?id=all in your browser.
2. Save the page as HTML, or copy the table into a `.csv` or `.txt` file, and put that file in this folder.
3. From the repository folder, run `python3 scripts/update_ufo_counts.py`.

The command updates the dashboard counts and deletes the file you saved. Do not commit anything else from this folder. There is no automated scraping, and nothing in this repository downloads nuforc.org.
