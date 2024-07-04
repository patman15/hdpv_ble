/** 
 * Emulate a Hunter Douglas PowerView cover device using ESP32
 * 
 * TODO:
 * - adding device to appartement does only work after long timeout, 
 *   as some feedback to "reset scene automations" is expected
 *
 * AUTHOR: patman15
 * LICENSE: GPLv2
 */

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#include <stdarg.h>
#include <arpa/inet.h>

#define NAME "myPVcover"

#define COVER_SERVICE_UUID "0000FDC1-0000-1000-8000-00805f9b34fb"
#define COVER_CHAR_UUID "CAFE1001-C0FF-EE01-8000-A110CA7AB1E0"
#define XXX_CHAR_UUID "CAFE1002-C0FF-EE01-8000-A110CA7AB1E0"

//#define FW_SERVICE_UUID "CAFE8000-C0FF-EE01-8000-A110CA7AB1E0"

#define BAT_SERVICE_UUID BLEUUID("180F")
#define BAT_CHAR_UUID BLEUUID("2A19")


#pragma pack(1)
struct header {
  uint8_t serviceID;
  uint8_t cmdID;
  uint8_t sequence;
  uint8_t data_len;
};

struct position {
  uint16_t pos1;
  uint16_t pos2;
  uint16_t pos3;
  uint16_t tilt;
  uint8_t velocity;
};

BLECharacteristic *pCharacteristic_cover, *pCharacteristic_fw, *pCharacteristic_unknown, *pCharacteristic_bat;
BLEServer *pServer = NULL;
bool deviceConnected = false;
bool oldDeviceConnected = false;

void Serialprintln(const char* input...) {
  va_list args;
  va_start(args, input);
  for(const char* i=input; *i!=0; ++i) {
    if(*i!='%') { Serial.print(*i); continue; }
    switch(*(++i)) {
      case '%': Serial.print('%'); break;
      case 's': Serial.print(va_arg(args, char*)); break;
      case 'd': Serial.print(va_arg(args, int), DEC); break;
      case 'b': Serial.print(va_arg(args, int), BIN); break;
      case 'x': Serial.print(va_arg(args, int), HEX); break;
      case 'f': Serial.print(va_arg(args, double), 2); break;
    }
  }
  Serial.println();
  va_end(args);
}

const char* decode_cmd(uint16_t cmd) {
  switch(cmd) {
    case 0x01:
      return "set position";
    case 0xBA:
      return "activate scene";
    default:
      return "ERR";
  }

}

void print_hex(uint8_t *value, uint8_t len) {
    for (int i = 0; i < len; i++) {
      Serial.print("0x");
      Serial.print(value[i], HEX);
      Serial.print(" ");
    }
    Serial.println("");
}

class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      Serial.print("connect ID: ");
      Serial.println(pServer->getConnId());
      /*pServer->updatePeerMTU(pServer->getConnId(), 310);
      Serial.print("MTU: ");
      Serial.println(pServer->getPeerMTU(pServer->getConnId()));*/
      deviceConnected = true;
      BLEDevice::startAdvertising();      
    };

    void onDisconnect(BLEServer* pServer) {
      Serial.println("disconnect.");
      deviceConnected = false;
    }

    void onMtuChanged(BLEServer* pServer, esp_ble_gatts_cb_param_t* param) {
      Serialprintln("MTU changed: %d", pServer->getPeerMTU(pServer->getConnId()));
    }
};

class coverCallbacks: public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *pCharacteristic) {
    uint8_t *value = pCharacteristic->getData();

    Serialprintln("Cover write %s:", pCharacteristic->toString().c_str());
    print_hex(value, pCharacteristic->getLength());

    struct header data;
    memcpy((void *) &data, value, 4);
    Serialprintln("SRV: %x, CMD %x, SEQ %x, LEN %x", data.serviceID, data.cmdID, data.sequence, data.data_len);

    switch ((data.serviceID << 8) | data.cmdID) {
      case 0xF701:
        // set position
        struct position pos;
        memcpy((void *) &pos, &value[4], data.data_len);
        Serialprintln("\tset position\tpos1 %f%%, pos2 %d, pos3 %d, tilt %d, velocity %d", pos.pos1/100.0, pos.pos2, pos.pos3, pos.tilt, pos.velocity);
        break;
      case 0xF7B8:
        // stop movement
        Serial.println("\tstop");
        break;
      case 0xF7BA:
        // activate scene
        Serialprintln("\tactivate scene\tscene #%d", (uint16_t) value[4]);
        break;
      case 0xFA5B:
        // get scene
        Serialprintln("\tget scene\tscene #%d", (uint16_t) value[4]);
        break;        
      case 0xFAEA:
        // Reset Scene Automations
        // FIXME! wrong return value!
        Serialprintln("\treset scene automations\t");
        uint8_t ret[]={0xFA, 0xEA, data.sequence, 0x1, 0x0};
        Serial.print("ret: ");
        print_hex(ret, 5);
        pCharacteristic->setValue(ret, 5);
        pCharacteristic->indicate();
        break;
    }
    Serial.println();
  }

  void onRead(BLECharacteristic *pCharacteristic) {
      Serialprintln("Cover read: %s", pCharacteristic->toString().c_str());
      Serial.println();      
  }  
};

class batteryCallbacks: public BLECharacteristicCallbacks {
  
  void onWrite(BLECharacteristic *pCharacteristic) {
    uint8_t *value = pCharacteristic->getData();

    Serialprintln("Battery write: %s:", pCharacteristic->toString().c_str());     
    print_hex(value, pCharacteristic->getLength());
    Serial.println();
  }    

  void onRead(BLECharacteristic *pCharacteristic) {
      Serialprintln("Battery read: %s", pCharacteristic->toString().c_str());      
      Serial.println();
  }
};

class genericCallbacks: public BLECharacteristicCallbacks {
  
  void onWrite(BLECharacteristic *pCharacteristic) {
    uint8_t *value = pCharacteristic->getData();

    Serialprintln("generic write %s:", pCharacteristic->toString().c_str());
    print_hex(value, pCharacteristic->getLength());
    Serial.println();    
  }    

  void onRead(BLECharacteristic *pCharacteristic) {
      Serialprintln("generic read %s.", pCharacteristic->toString().c_str());
      Serial.println();      
  }
};

void setup() {
  Serial.begin(115200);
  Serial.println(NAME " initializing ...");

  BLEDevice::init(NAME);
  Serialprintln("MTU: %d", BLEDevice::getMTU());

  // Create the BLE Server
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  // Create the BLE Service
  BLEService *pCovService = pServer->createService(COVER_SERVICE_UUID);
  // Create a BLE Characteristic
  pCharacteristic_cover = pCovService->createCharacteristic(
                      COVER_CHAR_UUID,
                      BLECharacteristic::PROPERTY_NOTIFY |
                      BLECharacteristic::PROPERTY_WRITE |
                      BLECharacteristic::PROPERTY_WRITE_NR
                    );
  pCharacteristic_cover->setCallbacks(new coverCallbacks());
  // Create a BLE Descriptor
  BLEDescriptor *pDesc1 = new BLEDescriptor("2901", 10);
  pDesc1->setValue("cover");
  pCharacteristic_cover->addDescriptor(new BLE2902());
  pCharacteristic_cover->addDescriptor(pDesc1);
  
  pCharacteristic_unknown = pCovService->createCharacteristic(
                      XXX_CHAR_UUID,
                      BLECharacteristic::PROPERTY_INDICATE |                      
                      BLECharacteristic::PROPERTY_WRITE |
                      BLECharacteristic::PROPERTY_WRITE_NR                      
                    );
  pCharacteristic_unknown->setCallbacks(new genericCallbacks());


  BLEService *pBatService = pServer->createService(BAT_SERVICE_UUID);
  
  pCharacteristic_bat = pBatService->createCharacteristic(
                      BAT_CHAR_UUID,
                      BLECharacteristic::PROPERTY_READ
                    );
  pCharacteristic_bat->setCallbacks(new batteryCallbacks());
  pBatService->addCharacteristic(pCharacteristic_bat);  
  uint8_t battery_level = 42;
  pCharacteristic_bat->setValue(&battery_level, 1);
  pCharacteristic_bat->addDescriptor(new BLE2902());

  // BLEService *pFWService = pServer->createService(FW_SERVICE_UUID);
  // pCharacteristic_fw = pCovService->createCharacteristic(
  //                     CHAR_FW_UUID,
  //                     BLECharacteristic::PROPERTY_READ |                      
  //                     BLECharacteristic::PROPERTY_WRITE |
  //                     BLECharacteristic::PROPERTY_WRITE_NR
  //                   );
  // pCharacteristic_fw->setCallbacks(new genericCallbacks());
  // pCharacteristic_fw->addDescriptor(new BLE2902());
  // pCharacteristic_fw->addDescriptor(pDesc2);
  //BLEDescriptor *pDesc2 = new BLEDescriptor("2901", 10);
  //pDesc2->setValue("firmware");

  // Start the service
  pCovService->start();
  //pFWService->start();

  // Start advertising
  BLEAdvertisementData AdvertisementData;
  const String manufacturerData = String("\x19\x08\x00\x00\x2A\x00\x00\x00\x00\x00\xA2",11);
  //                         Hunter Douglas ^^--^^          ^^ ID-Type
  AdvertisementData.setManufacturerData(manufacturerData);
  AdvertisementData.setPartialServices(BLEUUID(COVER_SERVICE_UUID));
  AdvertisementData.setFlags((1 << 2) | (1 << 1)); // [BR/EDR Not Supported] | [LE General Discoverable Mode]

  BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();

  pAdvertising->setAdvertisementData(AdvertisementData);
  
  BLEDevice::startAdvertising();

  Serial.println("Device " NAME " ready.");
}

void loop() {
  // put your main code here, to run repeatedly:
  // disconnecting
  if (!deviceConnected && oldDeviceConnected) {
    delay(500);                   // give the bluetooth stack the chance to get things ready
    pServer->startAdvertising();  // restart advertising
    Serial.println("start advertising");
    oldDeviceConnected = deviceConnected;
  }
  // connecting
  if (deviceConnected && !oldDeviceConnected) {
    // do stuff here on connecting
    oldDeviceConnected = deviceConnected;
  }  
}
