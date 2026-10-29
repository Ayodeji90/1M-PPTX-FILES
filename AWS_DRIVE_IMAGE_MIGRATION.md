# Move rendered images from Google Drive to Amazon S3

This moves already-rendered image batches. It does not run PPTX conversion, so no database is needed and the EC2 machine does not need the project's conversion-worker profile.

## Provisioned AWS resources (2026-10-29)

- Region: `us-east-1`
- Original all-in-one stack `pptx-image-migration-20261029` was deleted; its bucket was retained.
- Current host-only stack: `pptx-image-migration-host-20261029` (reuses the retained bucket)
- S3 bucket: `pptx-img-archive-448049817364-20261029` (private, encrypted, retained if the stack is deleted)
- Current EC2 host: `i-0def77fb09d1d9ef6` (`t3.small`, Amazon Linux 2023, encrypted 20 GiB gp3 root disk)
- Previous EC2 host `i-0361b09086e4da0a8` was terminated when the original stack was deleted.
- The host is online in Systems Manager, has no inbound security-group rules, and has an instance role limited to this bucket plus Session Manager.
- `rclone` and Python 3 are installed. The migration program is at `/opt/pptx-migration/migrate_drive_images_to_s3.py`.
- The root rclone config already has an `aws` remote using the EC2 instance role; no AWS access keys are stored on the VM.

The instance is currently running and incurs hourly compute and public IPv4 charges. To connect from a terminal with the authenticated AWS CLI:

```bash
aws ssm start-session --target i-0def77fb09d1d9ef6 --region us-east-1
```

The local environment currently lacks `session-manager-plugin`, which the AWS CLI requires for terminal sessions. You can either install it using [AWS's Ubuntu instructions](https://docs.aws.amazon.com/systems-manager/latest/userguide/install-plugin-debian-ubuntu.html), or start a browser-based session from the AWS Console under **Systems Manager → Session Manager → Start session**. The instance is already online there.

In the session, switch to root so the Google Drive config uses the same rclone config directory as the preconfigured S3 remote:

```bash
sudo -i
rclone config
```

Create a remote named `gdrive` of type `drive`. Leave client ID and secret blank. Choose the full Drive scope so the later, optional post-verification source deletion can work. When rclone asks about auto config, answer `n`. At its `config_token>` prompt, use a separate terminal on a computer with a browser to run `rclone authorize "drive"`; sign into the Google account that owns the source images, then paste the resulting token JSON directly into the SSM terminal. Do not paste the token into chat. The token will stay in `/root/.config/rclone/rclone.conf` on the VM.

After configuration, you can check access with `rclone lsd gdrive:`. Send me the exact Drive folder paths you want moved (root folder names, or selected batch folders under them). Repository configs mention possible image roots `PptxSweeper_Image_Delivery_VM1`, `PptxSweeper_Image_Delivery_VM2`, `PptxSweeper_Image_Delivery_VM3`, and `PptxSweeper_Delivery_Azure_Images`; confirm which exist and which should move. I could not list the Drive root from this environment, so I have not assumed these are all present or that all should be moved.

The S3 prefix can be `images/<source-folder>/`; no bucket naming or further AWS setup is needed. I will inventory the selected paths first and report exact object count, size, and storage estimate before any copy or Drive deletion.

After the transfer is verified, remove the temporary EC2 host, role, and security group while retaining the S3 bucket with:

```bash
aws cloudformation delete-stack --stack-name pptx-image-migration-host-20261029 --region us-east-1
```

## Proposed setup

- **Storage:** one private S3 general purpose bucket, S3 Standard, with the existing Drive batch folder structure under a prefix. Use the AWS Region you normally use (or the closest practical Region); region affects the rate. Do not use Glacier for this short 2–3 month holding period: Glacier Flexible Retrieval has a 90-day minimum storage duration and Deep Archive has a 180-day minimum. Standard-IA has a 30-day minimum and retrieval charges, so Standard is the simple choice unless you know the files will rarely be read.
- **VM:** EC2 `t3.small` (2 vCPU, 2 GiB RAM) is a reasonable low-cost starting point for rclone streaming. The migration does no rendering and needs little local disk; choose Ubuntu, encrypted 20 GiB gp3 root EBS, and stop/terminate the VM as soon as verified. If transfer runtime or memory becomes a bottleneck, `t3.medium` (4 GiB) is the next modest step. T3 Unlimited can charge CPU surplus credits under sustained CPU use; monitor this on a long transfer.
- **Authentication:** attach an EC2 instance role that can list the target bucket and write only to the chosen bucket/prefix. Let AWS CLI/rclone use that role; do not put access keys in the script or on the VM. Configure the Google Drive rclone remote on the VM using a secure token transfer from a trusted machine. Never commit the rclone config or token.
- **Network:** an EC2 public IPv4 address may incur a separate charge while allocated. Use Session Manager if it is already available, or restrict SSH to your current IP and release any Elastic IP after the migration.

S3 is object storage, so a database is unnecessary for this archive. Keep S3 Block Public Access enabled and default server-side encryption enabled. The migration creates no lifecycle deletion rule; delete or transition the objects later only when you decide their retention period is over.

## Estimate storage cost

AWS lists S3 Standard at **$0.023 per GB-month for the first 50 TB in US East (N. Virginia)**; check the [current S3 pricing page](https://aws.amazon.com/s3/pricing/) for the bucket's actual Region and request costs. At that rate, each 100 GB costs about $2.30/month, $4.60 for 2 months, or $6.90 for 3 months. Storage cost is approximately `dataset_GB × regional_rate × months`; the script below computes GB from the Drive object sizes. S3 PUT/LIST request charges are usually small but depend on file count. AWS charges no S3 data-transfer-in for uploads; EC2, EBS, public IPv4, taxes, and any data retrieval/egress are additional. S3 charges are usage-based; existing credits may cover eligible charges.

The repo cannot currently report the image root's total size without access to the authenticated Drive remote. Run the inventory first to get a dataset-specific number and estimate before copying:

```bash
python tools/migrate_drive_images_to_s3.py \
  --source gdrive:PptxSweeper_Image_Delivery_VM1 \
  --dest aws:YOUR_BUCKET/pptx-images \
  --months 3
```

This is read-only. Repeat `--source` for each root/folder you intend to move by running the command separately and reviewing each inventory. Pass `--storage-rate` if your S3 Region uses a different rate.

## Configure the VM

The host is Amazon Linux 2023, with rclone, Python 3, and AWS CLI already installed. Its `aws` rclone remote uses the attached instance role. Check role access with:

```bash
aws sts get-caller-identity
```

Create a bucket in the selected Region using the AWS console or CLI, with Block Public Access and default encryption enabled. Configure rclone's S3 remote to use the instance role:

```bash
rclone config create aws s3 provider AWS env_auth true region YOUR_REGION
rclone listremotes
```

Configure the Drive source remote as `gdrive` using rclone's headless OAuth authorization process. Keep `~/.config/rclone/rclone.conf` private (mode 600), then verify `rclone lsd gdrive:` and `rclone lsd aws:YOUR_BUCKET` before transfer.

## Copy, verify, and optionally remove the Drive copy

First run the inventory command above. If the source, size, object count, bucket, and cost estimate look right, run:

```bash
python tools/migrate_drive_images_to_s3.py \
  --source gdrive:PptxSweeper_Image_Delivery_VM1 \
  --dest aws:YOUR_BUCKET/pptx-images \
  --months 3 --copy
```

The copy resumes safely when rerun. After copying, it checks source/destination contents and then checks object counts and byte totals. By default verification compares file sizes; use `--verify-download` for content verification by reading both sides, which costs more time and data transfer. Source files remain in Drive on any copy or verification error.

Only when the S3 copy has been verified and you are ready to free Drive quota, add `--delete-source`. The script asks you to type the exact source path before it deletes anything. It permanently deletes the confirmed source files because Drive Trash still uses storage quota. Duplicate names are collapsed only when file size and a matching content hash prove the entries are identical; ambiguous duplicates stop the run and leave Drive intact:

```bash
python tools/migrate_drive_images_to_s3.py \
  --source gdrive:PptxSweeper_Image_Delivery_VM1 \
  --dest aws:YOUR_BUCKET/pptx-images \
  --months 3 --copy --verify-download --delete-source
```

Source deletion requires `--verify-download`; the script compares contents before allowing deletion.

Run per batch folder if you want a smaller, staged migration; preserve the folder path in the destination. Keep the VM until verification and any desired spot checks are complete. Then terminate the instance and remove its root EBS volume if it is no longer needed. S3 persists after the VM is gone.

## Cost caveats

This estimates only S3 Standard storage from the inventory total. It excludes S3 requests, taxes, internet retrieval/egress, the VM's running hours, EBS, public IPv4, and potential Google Drive API/quota behavior. EBS can continue billing after an instance stops; terminate the instance and confirm its disposable root volume was removed. Do not rely on credits as a substitute for checking Cost Explorer after the transfer.
