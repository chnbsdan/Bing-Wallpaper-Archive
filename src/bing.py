import dataclasses
import datetime
import os
import shutil
from shutil import rmtree
from typing import Any, Iterable

from anticorrupt import fetch_valid_image
from ApiPostprocessor import postprocessor
from Region import REGIONS, Region
from bing_utils import extract_base_url, get_uhd_url
from structures import ApiEntry, DATE_FORMAT
from system_utils import mkpath, posixpath, warn, fetch_json

# ★★★ 修改：尝试导入 R2，失败则禁用 ★★★
try:
    from cloudflare import CloudflareR2
    storage = CloudflareR2()
    print('✅ Cloudflare R2 initialized')
except ImportError:
    print('⚠️ CloudflareR2 module not found, storage disabled')
    storage = None
except Exception as e:
    print(f'⚠️ Cloudflare R2 initialization failed: {e}')
    storage = None


def parse_date(date_string: str) -> datetime.date | None:
    date_string = date_string.strip()

    if '_' in date_string:
        datetime_format = '%Y%m%d_%H%M'
        has_time = True
    elif len(date_string) == 12:
        datetime_format = '%Y%m%d%H%M'
        has_time = True
    elif len(date_string) == 8:
        datetime_format = '%Y%m%d'
        has_time = False
    else:
        warn(f'parse_date: unexpected date format: {date_string!r}')
        return None

    try:
        parsed = datetime.datetime.strptime(date_string, datetime_format)
        print(f'[Date parsing] parsed {date_string!r} with {datetime_format!r} -> {parsed}')
    except ValueError:
        warn(f'parse_date: cannot parse date string: {date_string!r}')
        return None

    if has_time and parsed.hour >= 15:
        parsed = parsed + datetime.timedelta(days=1)

    return parsed.date()


def add_entry(api_by_date: dict[datetime.date, ApiEntry], new_entry: ApiEntry) -> bool:
    """
    :return: True if the `api_by_date` was modified, False otherwise
    """

    date = new_entry.date

    if date not in api_by_date:
        api_by_date[date] = new_entry
        return True

    old_entry = api_by_date[date]

    if old_entry.bing_url != new_entry.bing_url:
        warn(f'Force-rewriting api for {date} due to Bing URL change:\n'
             f'"{old_entry.bing_url}" -> "{new_entry.bing_url}"')
        api_by_date[date] = new_entry
        return True

    updates: dict[str, Any] = {}

    for field in dataclasses.fields(ApiEntry):
        key = field.name
        if key == 'date':
            continue

        new_value = getattr(new_entry, key)
        if new_value is None:
            continue

        old_value = getattr(old_entry, key)
        if old_value is None:
            updates[key] = new_value
            continue

        if old_value == new_value:
            continue

        match key:
            case 'description':
                if old_value.startswith(new_value):
                    continue

            case 'title' | 'caption':
                new_value = new_value.replace('’', "'")

        if old_value != new_value:
            warn(f'Rewriting key `{key}` for {date}:\n{old_value}\nvs\n{new_value}')
            updates[key] = new_value

    if updates:
        api_by_date[date] = dataclasses.replace(old_entry, **updates)

    return bool(updates)


# ★★★ 上传图片函数 - 支持本地存储和 R2 ★★★
def upload_image(region: Region, date: datetime.date, bing_url: str) -> str:
    filename = date.strftime(DATE_FORMAT) + '.jpg'
    temp_image_path = mkpath('_temp', filename)

    content = fetch_valid_image(bing_url)
    with open(temp_image_path, 'wb') as file:
        file.write(content)

    # ★★★ 本地存储路径 ★★★
    local_dir = mkpath('api', 'images', region.api_country.upper(), region.api_lang.lower())
    os.makedirs(local_dir, exist_ok=True)
    local_path = os.path.join(local_dir, filename)

    # 复制图片到本地
    shutil.copy2(temp_image_path, local_path)
    print(f'✅ 图片已保存到本地: {local_path}')

    # ★★★ 如果 R2 可用，也上传到 R2 ★★★
    if storage is not None:
        try:
            r2_path = posixpath.join(region.api_country.upper(), region.api_lang.lower(), filename)
            new_url = storage.upload_file(
                temp_image_path,
                r2_path,
                skip_exists=False
            )
            print(f'✅ 图片已上传到 R2: {new_url}')
            return new_url
        except Exception as e:
            print(f'⚠️ R2 上传失败: {e}，使用本地路径')

    # ★★★ 返回本地路径（相对于仓库根目录） ★★★
    # 修复：使用 posixpath.join() 而不是 posixpath()
    relative_path = posixpath.join('api', 'images', region.api_country.upper(), region.api_lang.lower(), filename)
    print(f'📁 返回相对路径: {relative_path}')
    return relative_path


def update_from_hp_image_archive(region: Region, bing_url_mapping: dict[str, str]) -> Iterable[ApiEntry]:
    print('Getting caption, image and image url from bing.com/HPImageArchive.aspx...')

    data = fetch_json(
        'https://www.bing.com/HPImageArchive.aspx',
        params={'mkt': region, 'setlang': region.lang, 'cc': region.country, 'format': 'js', 'idx': 0, 'n': 100}
    )['images']

    for image_data in data:
        date = parse_date(image_data['fullstartdate'].strip())
        if date is None:
            warn(f'Cannot parse date:\n{image_data}')
            continue

        caption = image_data['title'].strip()
        bing_url = get_uhd_url(region, image_data['urlbase'].strip())

        if bing_url not in bing_url_mapping:
            bing_url_mapping[bing_url] = upload_image(region, date, bing_url)

        yield ApiEntry(
            date=date,
            caption=caption,
            bing_url=bing_url,
            url=bing_url_mapping[bing_url]
        )


def update_from_hp_api_model(region: Region, bing_url_mapping: dict[str, str]) -> Iterable[ApiEntry]:
    print('Getting title, caption, copyright, description and image url from bing.com/hp/api/model...')

    data = fetch_json(
        'https://www.bing.com/hp/api/model',
        params={'mkt': region, 'setlang': region.lang, 'cc': region.country}
    )['MediaContents']

    for image_data in data:
        date = parse_date(image_data['Ssd'].strip())
        if date is None:
            warn(f'Cannot parse date:\n{image_data}')
            continue

        title = image_data['ImageContent']['Title'].strip()
        caption = image_data['ImageContent']['Headline'].strip()
        copyright = image_data['ImageContent']['Copyright'].strip()
        base_url = extract_base_url(image_data['ImageContent']['Image']['Url'].strip())
        bing_url = get_uhd_url(region, base_url)

        description = image_data['ImageContent']['Description'].strip()
        description = description.replace('  ', ' ')  # Fix for double spaces

        if bing_url not in bing_url_mapping:
            bing_url_mapping[bing_url] = upload_image(region, date, bing_url)

        yield ApiEntry(
            date=date,
            title=title,
            caption=caption,
            copyright=copyright,
            description=description,
            bing_url=bing_url,
            url=bing_url_mapping[bing_url]
        )


def update_from_hp_image_gallery(region: Region, bing_url_mapping: dict[str, str]) -> Iterable[ApiEntry]:
    print('Getting title, subtitle, copyright, description and image url from bing.com/hp/api/v1/imagegallery...')
    data = fetch_json(
        'https://www.bing.com/hp/api/v1/imagegallery',
        params={'mkt': region, 'setlang': region.lang, 'cc': region.country, 'format': 'json'}
    )['data']['images']

    for image_data in data:
        date = parse_date(image_data['isoDate'].strip())
        if date is None:
            warn(f'Cannot parse date:\n{image_data}')
            continue

        title = image_data['title'].strip()
        subtitle = image_data['caption'].strip()
        copyright = image_data['copyright'].strip()

        description = image_data['description'].strip()
        i = 2
        while image_data.get(f'descriptionPara{i}') is not None:
            description += '\n' + image_data[f'descriptionPara{i}'].strip()
            i += 1
        description = description.strip().replace('  ', ' ')  # Fix for double spaces

        base_url = extract_base_url(image_data['imageUrls']['landscape']['ultraHighDef'].strip())
        bing_url = get_uhd_url(region, base_url)

        if bing_url not in bing_url_mapping:
            bing_url_mapping[bing_url] = upload_image(region, date, bing_url)

        yield ApiEntry(
            date=date,
            title=title,
            subtitle=subtitle,
            copyright=copyright,
            description=description,
            bing_url=bing_url,
            url=bing_url_mapping[bing_url]
        )


def update(region: Region):
    print(f'Updating {repr(region)}...')

    os.makedirs('_temp', exist_ok=True)

    api_by_date = {item.date: item for item in region.read_api()}
    bing_url_mapping = {}  # Force uploading (rewriting) all available images

    for update_func in (
        update_from_hp_image_archive,
        update_from_hp_api_model,
        update_from_hp_image_gallery
    ):
        for entry in update_func(region, bing_url_mapping):
            add_entry(api_by_date, entry)

    if os.path.isdir('_temp'):
        rmtree('_temp')

    region.write_api(
        postprocessor.process_api(list(api_by_date.values()))
    )
    print()


def update_all():
    for region in REGIONS:
        update(region)


# ---------------------------------------------------- Development -----------------------------------------------------

if __name__ == '__main__':
    update_all()
