# 技改备案材料采集与跨门户申报回执

合成政府政策网页采集、企业备案材料清单和外部申报门户回执，项目与机构均为虚构代号。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `policy.csv` contains 办事指南条款及采集到的修订版本。 Columns are `rule`, `category`, `location`, `min_investment`, `valid_from`, `revision`, `state`.
- `applications.csv` contains 企业项目事实、材料和创建时采用的规则。 Columns are `app`, `company`, `category`, `location`, `investment`, `rule`, `policy_revision`, `state`.
- `receipts.csv` contains 门户提交请求与回执记录。 Columns are `submission`, `app`, `portal_ref`, `files`, `state`, `received`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
