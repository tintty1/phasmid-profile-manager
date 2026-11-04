"""
Export and import profiles via Excel.
"""

import io
from typing import Any

from loguru import logger
from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from .models import BrowserSettings, Profile
from .profile_manager import ProfileManager


class ExcelManager:
    """Handle Excel export and import of profiles."""

    def __init__(self, profile_manager: ProfileManager):
        self.profile_manager = profile_manager

        # Columns for export: (field, header, help text). The machine itself
        # is not in the sheet: it is generated on the first launch of an
        # imported row, from these settings.
        self.columns = [
            ("id", "Profile ID", "Auto-generated ID (read-only)"),
            ("name", "Profile name", "Unique profile name"),
            ("group", "Group", "Profile group name"),
            ("status", "Status", "active or inactive"),
            ("os", "Operating system", "linux or windows"),
            ("gpu", "GPU", "vendor_id:device_id, e.g. 0x10de:0x2504; empty to draw one"),
            (
                "languages",
                "Browser languages",
                "Comma-separated: en-US, en; empty to follow the proxy",
            ),
            ("timezone", "Timezone", "For example: Europe/Berlin; empty to follow the proxy"),
            (
                "webrtc_policy",
                "WebRTC policy",
                "default, default_public_interface_only, default_public_and_private_interfaces "
                "or disable_non_proxied_udp",
            ),
            ("proxy_type", "Proxy type", "http, https, socks4, socks5 or empty"),
            ("proxy_server", "Proxy server", "host:port or empty"),
            ("proxy_username", "Proxy username", "Username or empty"),
            ("proxy_password", "Proxy password", "Password or empty"),
            ("notes", "Notes", "Additional information about the profile"),
        ]

    async def export_profiles_to_excel(self, profiles: list[Profile] | None = None) -> bytes:
        """Export profiles to an Excel file."""
        logger.info("Exporting profiles to Excel")

        if profiles is None:
            profiles = await self.profile_manager.list_profiles()

        # Create a new workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Browser profiles"

        # Configure styles
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")

        # Add headers
        for col_idx, (_field, header, comment_text) in enumerate(self.columns, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

            # Add an explanatory comment
            comment = Comment(comment_text, "PhasmidProfileManager")
            cell.comment = comment

        # Add profile rows
        for row_idx, profile in enumerate(profiles, 2):
            self._write_profile_row(ws, row_idx, profile)

        # Add data validation
        self._add_data_validation(ws, len(profiles) + 10)  # +10 rows for new profiles

        # Auto-size column widths
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except Exception:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width

        # Serialize to bytes
        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)

        logger.info(f"Export complete: {len(profiles)} profiles")
        return excel_buffer.getvalue()

    def _write_profile_row(self, ws, row_idx: int, profile: Profile):
        """Write a profile as a row in the worksheet."""
        browser_settings = profile.browser_settings
        proxy = profile.proxy

        # Prepare the row data
        row_data = {
            "id": profile.id,
            "name": profile.name,
            "group": profile.group or "",
            "status": profile.status.value
            if hasattr(profile.status, "value")
            else str(profile.status),
            "os": browser_settings.os,
            "gpu": browser_settings.gpu or "",
            "languages": ", ".join(browser_settings.languages or []),
            "timezone": browser_settings.timezone or "",
            "webrtc_policy": browser_settings.webrtc_policy,
            "proxy_type": proxy.type.value
            if proxy and hasattr(proxy.type, "value")
            else (str(proxy.type) if proxy else ""),
            "proxy_server": proxy.server if proxy else "",
            "proxy_username": proxy.username if proxy else "",
            "proxy_password": proxy.password if proxy else "",
            "notes": profile.notes or "",
        }

        # Write the values into the row
        for col_idx, (field, _, _) in enumerate(self.columns, 1):
            ws.cell(row=row_idx, column=col_idx, value=row_data.get(field, ""))

    def _add_data_validation(self, ws, max_row: int):
        """Add data validation for the columns."""
        # Status validation
        status_validation = DataValidation(
            type="list", formula1='"active,inactive"', allow_blank=True
        )
        ws.add_data_validation(status_validation)
        status_validation.add(f"D2:D{max_row}")

        def column(field: str) -> str:
            index = next(i for i, (name, _, _) in enumerate(self.columns) if name == field)
            return get_column_letter(index + 1)

        for field, choices in (
            ("os", "linux,windows"),
            (
                "webrtc_policy",
                "default,default_public_interface_only,"
                "default_public_and_private_interfaces,disable_non_proxied_udp",
            ),
            ("proxy_type", "http,https,socks4,socks5"),
        ):
            validation = DataValidation(type="list", formula1=f'"{choices}"', allow_blank=True)
            ws.add_data_validation(validation)
            letter = column(field)
            validation.add(f"{letter}2:{letter}{max_row}")

    async def import_profiles_from_excel(self, excel_data: bytes) -> dict[str, Any]:
        """Import profiles from an Excel file."""
        logger.info("Importing profiles from Excel")

        result: dict[str, Any] = {
            "success": True,
            "created_count": 0,
            "updated_count": 0,  # Always 0: import only creates new profiles
            "error_count": 0,
            "errors": [],
            "summary": "",
        }

        try:
            # Load the workbook
            excel_buffer = io.BytesIO(excel_data)
            wb = load_workbook(excel_buffer)
            ws = wb.active

            headers = self._locate_columns(ws)

            # Process the rows
            for row_idx in range(2, ws.max_row + 1):
                try:
                    await self._process_excel_row(ws, row_idx, headers, result)
                except Exception as e:
                    error_msg = f"Row {row_idx}: {e}"
                    result["errors"].append(error_msg)
                    result["error_count"] += 1
                    logger.error(error_msg)

            # Build the summary report
            result["summary"] = (
                f"Import complete:\n"
                f"Profiles created: {result['created_count']}\n"
                f"Errors: {result['error_count']}\n"
                f"All profiles are created with new auto-generated IDs"
            )

            if result["error_count"] == 0:
                logger.info(f"Import succeeded: created {result['created_count']} profiles")
            else:
                logger.warning(f"Import finished with {result['error_count']} errors")
                result["success"] = False

        except Exception as e:
            logger.error(f"Fatal import error: {e}")
            result["success"] = False
            result["errors"].append(f"Fatal error: {e}")
            result["summary"] = "Import failed due to a fatal error"

        return result

    def _locate_columns(self, ws) -> dict[str, int]:
        """Find which column each field occupies in *this* sheet.

        The layout used to be taken from ``self.columns`` by position, on the
        assumption that the file still looks exactly like the export. Bulk
        editing invites the opposite: a column dragged elsewhere, or a read-only
        one deleted. Every field after the change was then read from its
        neighbour's column, and because most of them are free text nothing
        objected — a sheet with Locale moved ahead of Timezone imported cleanly
        with the two values swapped, for every row.
        """
        present: dict[str, int] = {}
        for col_idx in range(1, ws.max_column + 1):
            value = ws.cell(row=1, column=col_idx).value
            if value is not None:
                present.setdefault(str(value).strip(), col_idx)

        located = {field: present[header] for field, header, _ in self.columns if header in present}
        if "name" not in located:
            name_header = next(header for field, header, _ in self.columns if field == "name")
            raise ValueError(
                f"No '{name_header}' column found in the first row. "
                "Export the profiles to Excel and edit that file."
            )
        return located

    async def _process_excel_row(
        self, ws, row_idx: int, headers: dict[str, int], result: dict[str, Any]
    ):
        """Process a single Excel row."""
        # Every field is read, whether or not the sheet still has its column; a
        # column the user removed leaves the value empty, which is what the
        # defaults below are for.
        row_data = {field: "" for field, _, _ in self.columns}
        for field, col_idx in headers.items():
            cell_value = ws.cell(row=row_idx, column=col_idx).value
            row_data[field] = str(cell_value).strip() if cell_value is not None else ""

        # Skip empty rows
        if not any(row_data.values()):
            return

        # Validate required fields
        if not row_data["name"]:
            raise ValueError("Profile name is required")

        # The ID is ignored: a new profile with an auto-generated ID is always created
        # This keeps IDs unique and sequential
        browser_settings = self._create_browser_settings(row_data)
        proxy_config = self._create_proxy_config(row_data)

        await self.profile_manager.create_profile(
            name=row_data["name"],
            group=row_data["group"] or None,
            browser_settings=browser_settings,
            proxy_config=proxy_config,
            # The first launch generates the machine: a spreadsheet of a hundred
            # rows should import in seconds, not wait on the generator per row.
            generate_fingerprint=False,
            # Both columns are exported and both were read here and then dropped
            # on the floor, so every import came back active and without notes.
            notes=row_data["notes"] or None,
            status=row_data["status"] or None,
        )
        result["created_count"] += 1

    def _create_browser_settings(self, row_data: dict[str, str]) -> BrowserSettings:
        """Build a BrowserSettings object from Excel data; empty cells keep the defaults."""
        values: dict[str, Any] = {"os": row_data["os"] or "linux"}
        if row_data["gpu"]:
            values["gpu"] = row_data["gpu"]
        if row_data["languages"]:
            values["languages"] = [lang.strip() for lang in row_data["languages"].split(",")]
        if row_data["timezone"]:
            values["timezone"] = row_data["timezone"]
        if row_data["webrtc_policy"]:
            values["webrtc_policy"] = row_data["webrtc_policy"]
        return BrowserSettings(**values)

    def _create_proxy_config(self, row_data: dict[str, str]) -> dict[str, Any] | None:
        """Build a proxy configuration from Excel data."""
        if not row_data["proxy_type"] or not row_data["proxy_server"]:
            return None

        return {
            "type": row_data["proxy_type"],
            "server": row_data["proxy_server"],
            "username": row_data["proxy_username"] or None,
            "password": row_data["proxy_password"] or None,
        }
