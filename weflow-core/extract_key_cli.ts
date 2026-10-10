import { KeyService } from './src/services/keyService';
import * as path from 'path';

const dbPath = process.argv[2];
const wxid = process.argv[3];
const accountDir = path.join(dbPath, wxid);

async function main() {
  const svc = new KeyService();
  const r = await svc.autoGetDbKey(600_000, (m: string) => console.log(`[key] ${m}`));
  if (!r.success) {
    console.error('DB_KEY_FAIL: ' + r.error);
    process.exit(1);
  }
  console.log('DB_KEY=' + r.key);
  try {
    const img = await svc.autoGetImageKey(accountDir, (m: string) => console.log(`[imgkey] ${m}`), wxid);
    if (img.success) {
      console.log('IMG_XOR=' + img.xorKey);
      console.log('IMG_AES=' + img.aesKey);
    } else {
      console.log('IMG_KEY_FAIL: ' + img.error);
    }
  } catch (e: any) {
    console.log('IMG_KEY_FAIL: ' + e.message);
  }
  process.exit(0);
}
main();
