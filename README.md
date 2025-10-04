# OmronConnection
Koneksi Omron seri Hem-7142t1 dengan Bluetooth dengan output API


pip install terminaltables bleak

python ./omblepy.py -p -d hem_7142t1

ipconfig
python -m uvicorn main:app --reload --host 192.666.66.666 --port 8000