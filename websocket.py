@app.websocket("/ws/bp-data")
async def connect_and_read_latest_ws(websocket: WebSocket):
    """
    WebSocket untuk pairing dan membaca data pengukuran terbaru dari perangkat Omron.
    """
    await websocket.accept()
    client = None  # Variabel untuk BLE client
    try:
        # Terima payload dari WebSocket client
        data = await websocket.receive_json()
        mac_address = data.get("mac_address")
        pairing = data.get("pairing", False)
        sync_time = data.get("sync_time", False)
        new_records_only = data.get("new_records_only", False)

        # Scan perangkat BLE
        devices = await BleakScanner.discover()
        selected_device = next((dev for dev in devices if dev.address == mac_address), None)
        if not selected_device:
            await websocket.send_json({"error": "Device not found during scan."})
            return

        client = BleakClient(selected_device.address)
        print("Device: ", selected_device.address, selected_device.name)

        # Koneksi dan pairing ke perangkat BLE
        await client.connect()
        await client.pair(protection_level=2)
        if not client.is_connected:
            await websocket.send_json({"error": "Failed to connect to the BLE device."})
            return

        # Inisialisasi handler BLE
        bluetoothTxRxObj = bluetoothTxRxHandler(client)
        dev_driver = deviceSpecificDriver()

        if pairing:
            # Pairing mode
            if dev_driver.deviceUseLockUnlock:
                await bluetoothTxRxObj.writeNewUnlockKey()

            await bluetoothTxRxObj.startTransmission()
            await bluetoothTxRxObj.endTransmission()
            await websocket.send_json({"message": "Pairing successful."})
        else:
            # Mulai komunikasi data
            await bluetoothTxRxObj.startTransmission()
            records = await dev_driver.getRecords(
                btobj=bluetoothTxRxObj,
                useUnreadCounter=new_records_only,
                syncTime=sync_time,
            )
            await bluetoothTxRxObj.endTransmission()
            if not records:
                await websocket.send_json({"error": "No records found."})
            else:
                latest_record = records[-1][-1]
                await websocket.send_json({
                    "message": "Newest record read with success.",
                    "mac_address": selected_device.address,
                    "device_name": selected_device.name,
                    "latest_record": latest_record
                })

        # Tunggu komunikasi tetap terbuka
        while True:
            try:
                # Terima data dari client (kalau ada)
                client_data = await websocket.receive_text()
                print(f"Client message: {client_data}")
            except WebSocketDisconnect:
                print("WebSocket client disconnected.")
                break

    except Exception as e:
        await websocket.send_json({"error": str(e)})
    finally:
        # Pastikan disconnect BLE
        if client and client.is_connected:
            await client.disconnect()
        await websocket.close()
