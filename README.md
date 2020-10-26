# Bookmate Downloader
Downloads books from https://bookmate.com and saves them as epub format files.

# You need to be subscribed to bookmate.com premium OR download books that are available for free !!!
Works on Mac OS X, Linux.
For Windows 10 you'll need to install WSL (Windows subsystem for linux) aka Windows Ubuntu.

Steps:
1. Buy the subscription at bookmate.com
2. Authorize at bookmate.com with your chrome browser
3. install python3
4. Copy the bookid (open the book at bookmate.com and check the url)
5. `python3 bookmate_downloader.py --bookid BookIdHere`
6. The epub will be downloaded to "out"

## Installation details:
```bash
sudo apt update
sudo apt install -y python3-pip
git clone https://github.com/ilyakharlamov/bookmate_downloader
cd bookmate_downloader
pip3 install -r src/python3/requirements.txt
python3 src/python3/bookmate_downloader.py --bookid KFHDG3bp
```
## Docker Usage (Alternative "installation")

```bash
docker build -t bookmate_dl .
docker run -it --rm -v -e BMS=<BMS COOKIE> {PWD}/:/data bookmate_dl --bookid KFHDG3bp
```