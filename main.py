from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import asyncio
from bleak import BleakClient, BleakScanner
from omblepy import bluetoothTxRxHandler, scanBLEDevices, appendCsv, saveUBPMJson
import logging
import os
json_path = os.path.join('ubpm.json')
import hashlib

# Memastikan driver spesifik tersedia
from deviceSpecific.hem_7142t1 import deviceSpecificDriver
from datetime import datetime, timezone
from copy import deepcopy


logger              = logging.getLogger("omblepy")
bleClient           = None
deviceSpecific = None
bleClient = None

app = FastAPI()
connected_clients = []

# Model untuk input pengguna
class BLEDevice(BaseModel):
    mac_address: str
    device_name: str

class ReadRecordsInput(BaseModel):
    mac_address: str
    new_records_only: bool = False
    sync_time: bool = False
    device_name: str
    
class ConnectAndReadInput(BaseModel):
    mac_address: str
    device_name: str
    new_records_only: bool
    sync_time: bool
    pairing: bool
    
RX_CHANNEL_UUIDS = [
    "49123040-aee8-11e1-a74d-0002a5d5c51b",
    "4d0bf320-aee8-11e1-a0d9-0002a5d5c51b",
    "5128ce60-aee8-11e1-b84b-0002a5d5c51b",
    "560f1420-aee8-11e1-8184-0002a5d5c51b",
]
ble_client = None 

def parse_device_dt(dt):
    """
    Terima string atau datetime. Return datetime object (naive, lokal).
    Menerima format "YYYY-MM-DD HH:MM:SS" dan ISO "YYYY-MM-DDTHH:MM:SS".
    """
    if isinstance(dt, datetime):
        return dt
    if not isinstance(dt, str):
        raise ValueError("unsupported datetime type: %r" % type(dt))
    # coba iso first (handles both 'T' and space in py3.11+)
    try:
        # Python 3.11/3.12: fromisoformat accepts both 'T' and ' '
        return datetime.fromisoformat(dt)
    except Exception:
        # fallback: common format with space
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(dt, fmt)
            except Exception:
                pass
    # kalau masih gagal, raise
    raise ValueError(f"Cannot parse datetime string: {dt!r}")

def normalize_records_datetime(records):
    """
    records: List[List[dict]] (per_user -> list of dicts)
    Parses each record['datetime'] into a datetime object and stores original as record['_device_datetime_raw'].
    Returns a NEW nested list (deepcopy) so source isn't mutated.
    """
    recs_copy = deepcopy(records)
    for per_user in recs_copy:
        for rec in per_user:
            if "datetime" in rec:
                raw = rec["datetime"]
                try:
                    dtobj = parse_device_dt(raw)
                except Exception:
                    # jika parsing gagal, set now() as fallback but keep raw
                    dtobj = datetime.now()
                rec["datetime"] = dtobj
    return recs_copy

def adjust_latest_to_today_non_destructive(latest_record, anchor_dt=None):
    """
    Return a copy of latest_record with date replaced to today's date, keep time.
    """
    if anchor_dt is None:
        anchor_dt = datetime.now()
    rec = deepcopy(latest_record)
    dt = rec["datetime"]
    # rec["_device_datetime_raw"] = rec.get("_device_datetime_raw", dt.isoformat())
    rec["datetime"] = dt.replace(year=anchor_dt.year, month=anchor_dt.month, day=anchor_dt.day)
    return rec

def generate_record_id(record): #Generate ID
    """Generate unique ID from datetime"""
    dt_str = record["datetime"].strftime("%Y%m%d%H%M%S") if isinstance(record["datetime"], datetime) else record["datetime"].replace("-", "").replace(":", "").replace(" ", "")
    # Tambahkan sys/dia/bpm untuk extra uniqueness
    unique_str = f"{dt_str}_{record['sys']}_{record['dia']}_{record['bpm']}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:12]  # 12 char hex


@app.get("/")
def read_root():
    return {"message": "Omron BLE Python Backend is running"}

@app.get("/scan")
async def scan_devices():
    """Memindai perangkat BLE."""
    try:
        devices = await scanBLEDevices()
        return {"devices": devices, "message": "Perangkat BLE berhasil dipindai"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tolong hidupkan bluetooth: {str(e)}")
    

@app.post("/latest-bp-records")
async def connect_and_read_latest(data: ConnectAndReadInput):
    """
    Menghubungkan ke perangkat Omron dan hanya membaca data pengukuran terbaru.
    Parameter:
    - `pairing`: Jika True, hanya melakukan pairing.
    - `sync_time`: Jika True, menyinkronkan waktu perangkat.
    """
    try:
        devices = await BleakScanner.discover()
        selected_device = next((dev for dev in devices if dev.address == data.mac_address), None)
        if not selected_device:
            raise HTTPException(status_code=404, detail="Device not found during scan.")
        
        client = BleakClient(selected_device.address)
        print("Device: ", selected_device.address, selected_device.name)
        try:
            await client.connect()
            await client.pair(protection_level=2)
            if not client.is_connected:
                raise HTTPException(status_code=500, detail="Failed to connect to the BLE device.")

            bluetoothTxRxObj = bluetoothTxRxHandler(client)
            dev_driver = deviceSpecificDriver()

            if data.pairing:
                if hasattr(dev_driver, "deviceUseLockUnlock") and dev_driver.deviceUseLockUnlock:
                # if dev_driver.deviceUseLockUnlock:
                    await bluetoothTxRxObj.writeNewUnlockKey()
                await bluetoothTxRxObj.startTransmission()
                await bluetoothTxRxObj.endTransmission()
                return { "message": "Pairing successful." }
            else:
                await bluetoothTxRxObj.startTransmission()
                records = await dev_driver.getRecords(
                    btobj=bluetoothTxRxObj,
                    useUnreadCounter=data.new_records_only,
                    # useUnreadCounter=True, # Hanya baca catatan terbaru
                    syncTime=data.sync_time,
                )

                normalized = normalize_records_datetime(records)
                flat = [rec for per_user in normalized for rec in per_user]
                            
                # Ambil yang terbaru berdasarkan datetime object
                latest_device_record = max(flat, key=lambda r: r["datetime"])

                # JANGAN adjust lagi - langsung pakai data asli
                # latest_corrected = adjust_latest_to_today_non_destructive(latest_device_record)

                # Generate ID
                latest_device_record["id"] = generate_record_id(latest_device_record)
                
                # serializable
                lr = dict(latest_device_record)
                if isinstance(lr["datetime"], datetime):
                    lr["datetime"] = lr["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                    # lr["datetime"] = lr["datetime"].isoformat()
                return {
                    "message": "Newest record read with success.",
                    "mac_address": selected_device.address,
                    "device_name": selected_device.name,
                    "latest_record": lr
                }
        finally:
            if client.is_connected:
                await client.disconnect()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/connect-and-read")
async def connect_and_read(data: ConnectAndReadInput):
    """
    Menghubungkan ke perangkat Omron, membaca data, dan menyimpannya ke CSV/JSON.
    Parameter:
    - `pairing`: Jika True, hanya melakukan pairing.
    - `new_records_only`: Jika True, hanya membaca catatan baru.
    - `sync_time`: Jika True, menyinkronkan waktu perangkat.
    """
    try:
        devices = await BleakScanner.discover()
        selected_device = next((dev for dev in devices if dev.address == data.mac_address), None)
        if not selected_device:
            raise HTTPException(status_code=404, detail="Device not found during scan.")
        
        client = BleakClient(selected_device.address)
        print("Device: ", selected_device.address, selected_device.name)
        try:
            await client.connect()
            await client.pair(protection_level=2)
            if not client.is_connected:
                raise HTTPException(status_code=500, detail="Failed to connect to the BLE device.")

            bluetoothTxRxObj = bluetoothTxRxHandler(client)
            dev_driver = deviceSpecificDriver()

            if data.pairing:
                if hasattr(dev_driver, "deviceUseLockUnlock") and dev_driver.deviceUseLockUnlock:
                    await bluetoothTxRxObj.writeNewUnlockKey()
                await bluetoothTxRxObj.startTransmission()
                await bluetoothTxRxObj.endTransmission()
                return { "message": "Pairing successful." }
            else:
                await bluetoothTxRxObj.startTransmission()
                records = await dev_driver.getRecords(
                    btobj=bluetoothTxRxObj,
                    useUnreadCounter=data.new_records_only,
                    syncTime=data.sync_time,
                )

                # HANYA normalize, JANGAN adjust
                normalized = normalize_records_datetime(records)

                

                #Tanpa save ke CSV & JSON 
                # Flatten dan SORT DULU berdasarkan datetime object
                all_records = []
                for user_records in normalized:
                    for rec in user_records:
                        all_records.append(rec)
                
                # Sort berdasarkan datetime object (TERBARU KE TERLAMA)
                all_records.sort(key=lambda r: r["datetime"], reverse=True)
                
                # BARU convert datetime ke string setelah sorting
                for rec in all_records:
                    rec["id"] = generate_record_id(rec) # Generate ID sebelum convert datetime ke string
                    if isinstance(rec["datetime"], datetime):
                        rec["datetime"] = rec["datetime"].strftime("%Y-%m-%d %H:%M:%S")

                return {
                    "message": "Data read successfully.",
                    "mac_address": selected_device.address,
                    "device_name": selected_device.name,
                    "records": all_records  # ✅ datetime sudah string
                }

                                # Simpan langsung tanpa koreksi waktu
                # appendCsv(normalized)
                # saveUBPMJson(normalized)

                # return {
                #     "message": "Data read successfully.",
                #     "mac_address": selected_device.address,
                #     "device_name": selected_device.name,
                #     "records": [r for u in normalized for r in u]
                # }
        finally:
            if client.is_connected:
                await client.disconnect()
    except Exception as e:
        import traceback
        print("TRACEBACK:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")