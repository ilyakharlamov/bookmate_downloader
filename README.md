# Bookmate Downloader
Downloads books as epub from https://bookmate.com and saves them as epub format files.

# You need to be subscribed to bookmate.com premium OR download books that are available for free !!!
I think it works on Mac OS X only at the moment.

Steps:
1. Buy the subscription at bookmate.com
2. Authorize at bookmate.com with your chrome browser
3. install python3
4. Copy the bookid
5. `python3 bookmate_downloader.py --bookid BookIdHere`
6. The epub will be downloaded to "out"

## Installation details:
```bash
git clone https://github.com/Rpsl/bookmate_downloader
cd bookmate_downloader
pip install -r requirements.txt
python bookmate_downloader.py
```
