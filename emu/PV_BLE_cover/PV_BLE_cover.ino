/** 
 * Emulate a Hunter Douglas PowerView cover device using ESP32
 * used e.g. to gain the home_key from an existing installation via BLE
 * 
 * TODO:
 * - cleanup code
 * - think about emulating a remote
 *
 * AUTHOR: patman15
 * LICENSE: GPLv2
 */

#define NAME "myPVcover"
const uint16_t SW_VERSION = 391;
const char *SERIAL_NR = "01234567890ABCDEF";
const uint16_t TYP_ID = 62;
const uint16_t MODEL_ID = 224;
const uint16_t FW_REVISION = 27;
const uint32_t HW_REVISION = 171103;
const uint8_t BATTERY_LEVEL = 42;

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define WOLFSSL_USER_SETTINGS
#include <wolfssl.h>
#include "wolfssl/wolfcrypt/aes.h"

Aes aes_coder;
void *hint = NULL;
int devId = INVALID_DEVID;  //if not using async INVALID_DEVID is default

#include <stdarg.h>
#include <arpa/inet.h>

#define COVER_SERVICE_UUID "0000FDC1-0000-1000-8000-00805f9b34fb"
#define COVER_CHAR_UUID "CAFE1001-C0FF-EE01-8000-A110CA7AB1E0"
#define XXX_CHAR_UUID "CAFE1002-C0FF-EE01-8000-A110CA7AB1E0"

#define FW_SERVICE_UUID "CAFE8000-C0FF-EE01-8000-A110CA7AB1E0"
#define FW_CHAR_UUID "CAFE8003-C0FF-EE01-8000-A110CA7AB1E0"

#define DEV_SERVICE_UUID BLEUUID("180A")
#define SER_CHAR_UUID BLEUUID("2A25")
#define MAN_CHAR_UUID BLEUUID("2A29")
#define MOD_CHAR_UUID BLEUUID("2A24")
#define FWR_CHAR_UUID BLEUUID("2A26")
#define HWR_CHAR_UUID BLEUUID("2A27")
#define SWR_CHAR_UUID BLEUUID("2A28")

#define BAT_SERVICE_UUID BLEUUID("180F")
#define BAT_CHAR_UUID BLEUUID("2A19")

#define DAT_LEN 255
#pragma pack(1)
struct message {
  uint8_t serviceID;
  uint8_t cmdID;
  uint8_t sequence;
  uint8_t data_len;
  uint8_t data[DAT_LEN];
};

struct position {
  uint16_t pos1;
  uint16_t pos2;
  uint16_t pos3;
  uint16_t tilt;
  uint8_t velocity;
};

struct notification {
  uint8_t *data;
  BLECharacteristic *characteristic;
};

BLECharacteristic *pCharacteristic_cover, *pCharacteristic_fw, *pCharacteristic_unknown, *pCharacteristic_bat;
BLECharacteristic *pCharacteristic_dev, *pCharacteristic_ser, *pCharacteristic_man, *pCharacteristic_mod, *pCharacteristic_fwr, *pCharacteristic_hwr;
BLEServer *pServer = NULL;
bool deviceConnected = false;
bool oldDeviceConnected = false;
struct notification rx_data;
volatile bool data_available = false;
const byte zero_key[16] = { 0 };
byte home_key[16] = { 0 };


const char *BLEstate[] = {
  "SUCCESS_INDICATE",
  "SUCCESS_NOTIFY",
  "ERROR_INDICATE_DISABLED",
  "ERROR_NOTIFY_DISABLED",
  "ERROR_GATT",
  "ERROR_NO_CLIENT",
  "ERROR_INDICATE_TIMEOUT",
  "ERROR_INDICATE_FAILURE"
};

void print_hex(const uint8_t *value, uint8_t len, const char *prefix = "0x", const char *postfix = " ") {
  for (int i = 0; i < len; i++) {
    Serial.printf("%s%02X%s", prefix, value[i], postfix);
  }
  Serial.println();
}

uint8_t set_response(message *response, const message *request, const byte *data = NULL, const uint8_t data_len = 1) {
  const uint8_t message_len = min(data_len, (uint8_t)DAT_LEN) + sizeof(struct message) - DAT_LEN;
  response->serviceID = request->serviceID & 0xEF;
  response->cmdID = request->cmdID;
  response->sequence = request->sequence;
  response->data_len = min(data_len, (uint8_t)DAT_LEN);
  if (data) {
    memcpy(response->data, data, std::min(data_len, (uint8_t)DAT_LEN));
  } else {
    *response->data = 0x0;
  }
  Serial.printf("\tret value (%i): ", message_len);
  print_hex((const uint8_t *)response, message_len);
  if (memcmp(home_key, zero_key, sizeof(zero_key))) {
    message unencrypted;
    memcpy(&unencrypted, response, message_len);
    // AES counter is reset every message, so we need to init it each time
    if (wc_AesInit(&aes_coder, hint, devId) || wc_AesSetKey(&aes_coder, (const byte *)home_key, 16, zero_key, AES_ENCRYPTION)) {
      Serial.println("FATAL: setting AES init failed!");
      return 0;
    }
    if (wc_AesCtrEncrypt(&aes_coder, (byte *)response, (const byte *)&unencrypted, message_len)) {
      Serial.println(F("FATAL: encryption failed!"));
      return 0;
    }
    Serial.printf("\tencrypted (%i): ", message_len);
    print_hex((const uint8_t *)response, message_len);
  }
  return message_len;
}

void decode(BLECharacteristic *pChar) {
  message response;
  byte data_dec[DAT_LEN];
  const uint16_t data_len = pChar->getLength();
  const byte *data_raw = pChar->getData();
  struct message msg;
  uint8_t resp_size = 0;

  Serial.print("\t BLE data: ");
  print_hex(data_raw, data_len);

  if (data_len < 4) return;

  if (memcmp(home_key, zero_key, sizeof(zero_key))) {
    if (wc_AesInit(&aes_coder, hint, devId) || wc_AesSetKey(&aes_coder, (const byte *)home_key, 16, zero_key, AES_ENCRYPTION)) {
      Serial.println("FATAL: setting AES init failed!");
    }
    if (wc_AesCtrEncrypt(&aes_coder, data_dec, data_raw, data_len)) {
      Serial.println(F("FATAL: decryption failed!"));
      return;
    }
    Serial.print("\tdecrypted: ");
    print_hex(data_dec, data_len);
  } else {
    memcpy(data_dec, data_raw, data_len);
  }

  memcpy((void *)&msg, data_dec, 4);
  Serial.printf("\t  message: SRV: %02x, CMD %02x, SEQ %i, LEN %i\n", msg.serviceID, msg.cmdID, msg.sequence, msg.data_len);

  // sepecial responses (static data!)
  const byte ret_valF1DD[] = { 0x00, 0x04, 0x01, 0x00, 0x00, 0x00, 0x87, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };                                                                                                                                                // product info
  const byte ret_valFFDD[] = { 0x00, 0x05, 0xd1, 0xa2, 0x9a, 0x42, 0x59, 0x5d, 0x5c, 0x52, 0x1b, 0x00, 0x00, 0x00, (uint8_t)(SW_VERSION & 0xFF), (uint8_t)(SW_VERSION >> 8), 0x00, 0x00, 0x5f, 0x9c, 0x02, 0x00, 0x5f, 0x9c, 0x02, 0x00, TYP_ID, MODEL_ID, 0x08 };  // HW diagnostics
  const byte ret_valFFDE[] = { 0x08, 0x00, 0x02, 0x26, 0x72, 0x01, 0x59, 0x01, 0x00 };                                                                                                                                                                              // power status
  const byte ret_valFA5B[] = { 0x00, 0x0a, 0xa2, 0x88, 0x13, 0x00, 0x80, 0x00, 0x80, 0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };                                                                                                                        // get scene
  const byte ret_valFA5A[] = { 0x00, 0x02, 0xb0 };                                                                                                                                                                                                                  // set scene

  Serial.print("\t\t");
  switch ((msg.serviceID << 8) | msg.cmdID) {
    case 0xF1DD:
      Serial.println("get product info.");
      resp_size = set_response(&response, (const message *)data_dec, ret_valF1DD, sizeof(ret_valF1DD));
      break;
    case 0xF701:
      // set position
      struct position pos;
      memcpy((void *)&pos, &data_dec[4], msg.data_len);
      Serial.printf("set position: pos1 %f%%, pos2 %d, pos3 %d, tilt %d, velocity %d\n", pos.pos1 / 100.0, pos.pos2, pos.pos3, pos.tilt, pos.velocity);
      break;
    case 0xF711:
      // identify
      Serial.printf("identify: %i times\n", data_dec[4]);
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xF7B8:
      // stop movement
      Serial.println("stop.");
      break;
    case 0xF7BA:
      // activate scene
      Serial.printf("activate scene #%i\n", data_dec[4]);
      break;
    case 0xFA5A:
      // set scene
      Serial.printf("set scene #%i\n", data_dec[4]);
      resp_size = set_response(&response, (const message *)data_dec, ret_valFA5A, sizeof(ret_valFA5A));
      break;
    case 0xFA5B:
      // get scene
      Serial.printf("get scene #%i\n", data_dec[4]);
      resp_size = set_response(&response, (const message *)data_dec, ret_valFA5B, sizeof(ret_valFA5B));
      break;
    case 0xFAEA:
      // Reset Scene Automations
      Serial.println("reset scene automations:");
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xFB02:
      // set shade key
      Serial.print("set shade key: ");
      print_hex(&data_raw[4], data_len - 4, "\\x", "");
      // set resonse before key, to acknowledge unencrypted
      resp_size = set_response(&response, (const message *)data_dec);
      if (msg.data_len == 16) {
        memcpy(home_key, &data_raw[4], 16);
      }
      break;
    // case 0xFF67:
    //   // get shade time
    //   break;
    case 0xFF77:
      // set shade time
      Serial.printf("set time: %i-%i-%i %i:%i:%i\n", data_dec[4] | data_dec[5] << 8, data_dec[6], data_dec[7], data_dec[8], data_dec[9], data_dec[10]);
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xFF87:
      Serial.printf("set sunrise %i:%i:%i, sunset %i:%i:%i\n", data_dec[4], data_dec[5], data_dec[6], data_dec[7], data_dec[8], data_dec[9]);
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xFFD7:
      Serial.printf("set shade configuration: 0x%02X, status LED: %s\n", data_dec[4], data_dec[5] ? "on" : "off");
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xFFDD:
      // get HW diagnostics
      Serial.println("get HW diagnostics.");
      resp_size = set_response(&response, (const message *)data_dec, ret_valFFDD, sizeof(ret_valFFDD));
      break;
    case 0xFFDE:
      // get power status
      Serial.println("get power status.");
      resp_size = set_response(&response, (const message *)&data_dec, ret_valFFDE, sizeof(ret_valFFDE));
      break;
    case 0xFFDF:
      // set power type
      Serial.printf("set power type: %i\n", data_dec[4]);
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    case 0xFFEE:
      Serial.println("factory reset.");
      resp_size = set_response(&response, (const message *)data_dec);
      break;
    default:
      Serial.println(F("*********************************** unknown message"));
  }
  if (resp_size) {
    pChar->setValue((uint8_t *)&response, resp_size);
    pChar->notify();
  }
}

class MyServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *pServer) {
    digitalWrite(LED_BUILTIN, HIGH);
    Serial.printf("connect ID: %i\n", pServer->getConnId());
    deviceConnected = true;
    BLEDevice::startAdvertising();
  };

  void onDisconnect(BLEServer *pServer) {
    digitalWrite(LED_BUILTIN, LOW);
    Serial.printf("disconnect ID: %i\n\n", pServer->getConnId());
    deviceConnected = false;
  }

  void onMtuChanged(BLEServer *pServer, esp_ble_gatts_cb_param_t *param) {
    Serial.printf("MTU changed: %d\n", pServer->getPeerMTU(pServer->getConnId()));
  }
};

class coverCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *pCharacteristic) {
    Serial.printf("Cover write %s\n", pCharacteristic->toString().c_str());
    decode(pCharacteristic);
  }

  void onRead(BLECharacteristic *pCharacteristic) {
    Serial.printf("Cover read: %s\n", pCharacteristic->toString().c_str());
  }

  void onNotify(BLECharacteristic *pCharacteristic) {
    Serial.printf("Cover onNotify() %s\n", pCharacteristic->toString().c_str());
  }

  void onStatus(BLECharacteristic *pCharacteristic, Status s, uint32_t code) {
    Serial.printf("Cover onStatus() %s: %s\n", BLEstate[s], pCharacteristic->toString().c_str());
  }
};

class batteryCallbacks : public BLECharacteristicCallbacks {

  void onWrite(BLECharacteristic *pCharacteristic) {
    uint8_t *value = pCharacteristic->getData();

    Serial.printf("Battery write: %s:", pCharacteristic->toString().c_str());
    print_hex(value, pCharacteristic->getLength());
    Serial.println();
  }

  void onRead(BLECharacteristic *pCharacteristic) {
    Serial.printf("Battery read: %s\n", pCharacteristic->toString().c_str());
  }

  void onNotify(BLECharacteristic *pCharacteristic) {
    Serial.println("Battery onNotify()");
  }
  void onStatus(BLECharacteristic *pCharacteristic, Status s, uint32_t code) {
    Serial.println("Battery onStatus()");
  }
};

class genericCallbacks : public BLECharacteristicCallbacks {

  void onWrite(BLECharacteristic *pCharacteristic) {
    //uint8_t *value = pCharacteristic->getData();

    Serial.printf("generic write %s:\n", pCharacteristic->toString().c_str());
    //print_hex(value, pCharacteristic->getLength());
  }

  void onRead(BLECharacteristic *pCharacteristic) {
    Serial.printf("generic read %s.\n", pCharacteristic->toString().c_str());
  }
  void onNotify(BLECharacteristic *pCharacteristic) {
    Serial.printf("generic onNotify() %s\n", pCharacteristic->toString().c_str());
  }  // not used
  void onStatus(BLECharacteristic *pCharacteristic, Status s, uint32_t code) {
    Serial.printf("generic onStatus() %s - %s\n", BLEstate[s], pCharacteristic->toString().c_str());
  };  // not used
};

void setup() {
  Serial.begin(115200);
  Serial.println(NAME " initializing ...");

  BLEDevice::init(NAME);
  // Create the BLE Server
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new MyServerCallbacks());

  // Create the BLE Service
  BLEService *pCovService = pServer->createService(COVER_SERVICE_UUID);
  // Create a BLE Characteristic
  pCharacteristic_cover = pCovService->createCharacteristic(
    COVER_CHAR_UUID,
    BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  pCharacteristic_cover->setCallbacks(new coverCallbacks());
  // Create a BLE Descriptor
  /* BLEDescriptor *pDesc1 = new BLEDescriptor("2901", 10);
pDesc1->setValue("cover");*/
  //pCharacteristic_cover->addDescriptor(pDesc1);
  pCharacteristic_cover->addDescriptor(new BLE2902());


  pCharacteristic_unknown = pCovService->createCharacteristic(
    XXX_CHAR_UUID,
    BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  pCharacteristic_unknown->setCallbacks(new genericCallbacks());
  pCharacteristic_unknown->addDescriptor(new BLE2902());

  BLEService *pBatService = pServer->createService(BAT_SERVICE_UUID);
  pCharacteristic_bat = pBatService->createCharacteristic(
    BAT_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_bat->setCallbacks(new batteryCallbacks());
  pCharacteristic_bat->setValue((uint8_t *)&BATTERY_LEVEL, 1);
  pCharacteristic_bat->addDescriptor(new BLE2902());

  BLEService *pFWService = pServer->createService(FW_SERVICE_UUID);
  pCharacteristic_fw = pFWService->createCharacteristic(
    FW_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  pCharacteristic_fw->setCallbacks(new genericCallbacks());
  pCharacteristic_fw->addDescriptor(new BLE2902());
  BLEDescriptor *pDesc2 = new BLEDescriptor("2901", 10);
  pDesc2->setValue("firmware");
  pCharacteristic_fw->addDescriptor(pDesc2);


  BLEService *pDEVService = pServer->createService(DEV_SERVICE_UUID);
  pCharacteristic_dev = pDEVService->createCharacteristic(
    SWR_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_dev->setValue(String(SW_VERSION));
  pCharacteristic_dev->setCallbacks(new genericCallbacks());
  pCharacteristic_ser = pDEVService->createCharacteristic(
    SER_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_ser->setValue(SERIAL_NR);
  pCharacteristic_ser->setCallbacks(new genericCallbacks());

  pCharacteristic_man = pDEVService->createCharacteristic(
    MAN_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_man->setValue("Hunter Douglas");
  pCharacteristic_man->setCallbacks(new genericCallbacks());
  pCharacteristic_mod = pDEVService->createCharacteristic(
    MOD_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_mod->setValue(String(TYP_ID));
  pCharacteristic_mod->setCallbacks(new genericCallbacks());  
    pCharacteristic_fwr = pDEVService->createCharacteristic(
    FWR_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_fwr->setValue(String(FW_REVISION));
  pCharacteristic_fwr->setCallbacks(new genericCallbacks());  
  pCharacteristic_hwr = pDEVService->createCharacteristic(
    HWR_CHAR_UUID,
    BLECharacteristic::PROPERTY_READ);
  pCharacteristic_hwr->setValue(String(HW_REVISION));
  pCharacteristic_hwr->setCallbacks(new genericCallbacks());  
 

  // Start the services
  pCovService->start();
  pFWService->start();
  pBatService->start();
  pDEVService->start();

  // Start advertising
  BLEAdvertisementData AdvertisementData;
  const char adv[] = {0x19, 0x08, 0x00, 0x00, TYP_ID, 0x00, 0x00, 0x00, 0x00, 0x00, 0xA2};
  //     Hunter Douglas ^^ -- ^^    ^^key ^^          ^--pos1--^
 
  AdvertisementData.setManufacturerData(String(adv, 11));
  AdvertisementData.setPartialServices(BLEUUID(COVER_SERVICE_UUID));
  AdvertisementData.setFlags((1 << 2) | (1 << 1));  // [BR/EDR Not Supported] | [LE General Discoverable Mode]

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
