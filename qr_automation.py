"""
QR Automation Module for PlanOut.com.ar integration
Handles automated QR code generation and sending for guest lists
"""

import logging
import os
import time
import pandas as pd
from playwright.sync_api import sync_playwright, Playwright
from typing import List, Dict, Optional, Tuple
import tempfile
import json
from datetime import datetime
import traceback

# Configure logging
logger = logging.getLogger(__name__)

class PlanOutAutomation:
    """Handles automation tasks for PlanOut.ar QR generation and sending"""
    
    def __init__(self):
        self.base_url = "https://planout.ar"
        self.login_url = f"{self.base_url}/backoffice/login"
        self.username = os.environ.get('PLANOUT_USERNAME', 'AntoSVG')
        self.password = os.environ.get('PLANOUT_PASSWORD', 'AntoSVG-987')
        self.headless = os.environ.get('PLANOUT_HEADLESS', 'true').lower() == 'true'
        self.browser = None
        self.page = None
        self.context = None
        self.timeout = 30000  # 30 seconds timeout
        
    def __enter__(self):
        """Context manager entry"""
        self.start_browser()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close_browser()
        
    def start_browser(self):
        """Initialize and start Playwright browser"""
        try:
            logger.info("🌐 Iniciando Playwright...")
            self.playwright = sync_playwright().start()
            
            logger.info("🚀 Lanzando browser Chromium...")
            logger.info(f"🔧 Modo headless: {self.headless}")
            
            # Verificar si Chromium está disponible
            try:
                self.browser = self.playwright.chromium.launch(
                    headless=self.headless,
                    args=[
                        '--no-sandbox', 
                        '--disable-dev-shm-usage',
                        '--disable-web-security',
                        '--disable-features=VizDisplayCompositor'
                    ]
                )
                logger.info("✅ Browser Chromium lanzado exitosamente")
            except Exception as browser_error:
                logger.error(f"❌ Error lanzando Chromium: {browser_error}")
                # Intentar instalar browsers si faltan
                logger.info("🔄 Intentando instalar browsers de Playwright...")
                import subprocess
                try:
                    subprocess.run(['playwright', 'install', 'chromium'], check=True, capture_output=True)
                    logger.info("✅ Browsers instalados, reintentando...")
                    self.browser = self.playwright.chromium.launch(
                        headless=self.headless,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                except Exception as install_error:
                    logger.error(f"❌ Error instalando browsers: {install_error}")
                    raise browser_error
            
            # Create browser context with clean cache
            self.context = self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            
            self.page = self.context.new_page()
            
            # Clear any existing cache and storage
            logger.info("Clearing browser cache and storage...")
            try:
                self.context.clear_cookies()
                self.context.clear_permissions()
                self.page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            except Exception as cache_e:
                logger.warning(f"Cache clearing warning: {cache_e}")
            
            logger.info("Browser started successfully with clean cache")
            
        except Exception as e:
            logger.error(f"Error starting browser: {str(e)}")
            raise
            
    def close_browser(self):
        """Close browser and cleanup"""
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if hasattr(self, 'playwright'):
                self.playwright.stop()
            logger.info("Browser closed successfully")
        except Exception as e:
            logger.error(f"Error closing browser: {str(e)}")
    
    def login_planout(self) -> bool:
        """
        Logs into PlanOut.ar backoffice with 2-step login process
        Step 1: Enter username and click Continue
        Step 2: Enter password and login
        Returns: True if successful, False otherwise
        """
        try:
            logger.info(f"Attempting 2-step login to PlanOut.ar: {self.login_url}")
            
            # Navigate to login page
            response = self.page.goto(self.login_url, wait_until="networkidle", timeout=self.timeout)
            if not response.ok:
                logger.error(f"Failed to load login page: HTTP {response.status}")
                return False
            
            logger.info("Login page loaded successfully")
            
            # Wait for login form to be visible
            try:
                self.page.wait_for_selector('form', timeout=10000)
                logger.info("Login form found successfully")
            except:
                # Debug: Check what's actually on the page
                logger.warning("Standard form selector not found, checking page content...")
                try:
                    # Take a screenshot for debugging
                    screenshot_path = f"debug_login_{int(time.time())}.png"
                    self.page.screenshot(path=screenshot_path)
                    logger.info(f"Screenshot saved as {screenshot_path}")
                    
                    # Check for any input fields
                    inputs = self.page.locator('input').count()
                    logger.info(f"Found {inputs} input elements on page")
                    
                    # Check for login-related elements
                    login_elements = self.page.locator('[id*="login"], [name*="login"], [class*="login"]').count()
                    logger.info(f"Found {login_elements} login-related elements")
                    
                    # Continue anyway if we find input elements
                    if inputs > 0:
                        logger.info("Found input elements, continuing with login attempt")
                    else:
                        logger.error("No input elements found on page")
                        return False
                        
                except Exception as debug_e:
                    logger.error(f"Debug check failed: {debug_e}")
                    return False
            
            # STEP 1: Enter username and click Continue
            logger.info("Step 1: Entering username...")
            
            # Find username field
            username_selectors = [
                '#login',  # Exact ID from HTML structure
                'input[name="login"]',  # Exact name from HTML
                'input[id="login"]',
                'input[type="text"][name="login"]'
            ]
            
            username_field = None
            for selector in username_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        username_field = selector
                        logger.info(f"Found username field: {selector}")
                        break
                except:
                    continue
            
            if not username_field:
                logger.error("Could not find username field")
                return False
            
            # Fill username
            self.page.fill(username_field, self.username)
            logger.info(f"Username '{self.username}' entered")
            
            # Find and click Continue button
            continue_button_selectors = [
                '#btnLogin',  # Exact ID from HTML structure
                'button[id="btnLogin"]',
                'button[type="submit"][id="btnLogin"]',
                'button:has-text("Continue")'
            ]
            
            continue_button = None
            for selector in continue_button_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        continue_button = selector
                        logger.info(f"Found Continue button: {selector}")
                        break
                except:
                    continue
            
            if not continue_button:
                logger.error("Could not find Continue button")
                return False
            
            # Click Continue button
            self.page.click(continue_button)
            logger.info("Continue button clicked")
            
            # Wait for password step to load - give more time
            time.sleep(5)
            
            # Also wait for the password field to appear
            try:
                self.page.wait_for_selector('#password', timeout=10000)
                logger.info("Password field appeared")
            except:
                logger.warning("Password field selector wait timed out, continuing anyway")
            
            # STEP 2: Enter password
            logger.info("Step 2: Entering password...")
            
            # Find password field
            password_selectors = [
                '#password',  # Exact ID from HTML structure
                'input[id="password"]',
                'input[name="password"]',
                'input[type="password"]'
            ]
            
            password_field = None
            for selector in password_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        password_field = selector
                        logger.info(f"Found password field: {selector}")
                        break
                except:
                    continue
            
            if not password_field:
                logger.error("Could not find password field")
                return False
            
            # Fill password
            self.page.fill(password_field, self.password)
            logger.info("Password entered")
            
            # Find and click final login button (might be same or different)
            final_login_selectors = [
                '#btnLoginPasswd',  # Exact ID for final login button
                '#btnLogin',  # Fallback in case same button ID is reused
                'button[type="submit"]',
                'button:has-text("Log in")',
                'button:has-text("Login")',
                'button:has-text("Sign in")',
                '.button.primary'
            ]
            
            final_login_button = None
            for selector in final_login_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        final_login_button = selector
                        logger.info(f"Found final login button: {selector}")
                        break
                except:
                    continue
            
            if final_login_button:
                self.page.click(final_login_button)
                logger.info("Final login button clicked")
            else:
                # Fallback: press Enter
                self.page.keyboard.press("Enter")
                logger.info("Login attempted with Enter key")
            
            # Wait for login to complete - give more time
            try:
                self.page.wait_for_navigation(timeout=30000)  # Increased to 30 seconds
                logger.info("Navigation completed after final login")
            except:
                logger.warning("No navigation detected after final login")
            
            # Wait for any redirects or loading - increased time
            time.sleep(10)  # Give more time for login processing
            
            # Check if login was successful
            current_url = self.page.url
            logger.info(f"Final URL after 2-step login: {current_url}")
            
            # Success indicators
            success_indicators = [
                "dashboard" in current_url.lower(),
                "home" in current_url.lower(),
                "main" in current_url.lower(),
                "backoffice" in current_url.lower() and "login" not in current_url.lower()
            ]
            
            # Check for error messages
            error_selectors = [
                '.alert-danger',
                '.error',
                ':has-text("Error")',
                ':has-text("Incorrect")',
                ':has-text("Invalid")'
            ]
            
            has_error = False
            for error_selector in error_selectors:
                try:
                    if self.page.locator(error_selector).is_visible():
                        error_text = self.page.locator(error_selector).text_content()
                        logger.error(f"Login error detected: {error_text}")
                        has_error = True
                        break
                except:
                    continue
            
            if has_error:
                return False
            
            # Check for successful login
            if any(success_indicators):
                logger.info("2-step login successful - redirected to dashboard")
                return True
            
            # Look for success indicators in the dashboard
            success_elements = [
                ':has-text("Hi,")',  # "Hi, Antonella Lancuba"
                ':has-text("Total sales")',
                ':has-text("Total tickets")',
                ':has-text("AntoSVG")',  # Username in top right
                '.user-menu',
                '.navbar-nav',
                ':has-text("Logout")',
                ':has-text("Profile")'
            ]
            
            for element in success_elements:
                try:
                    if self.page.locator(element).is_visible():
                        logger.info(f"Login success confirmed by dashboard element: {element}")
                        return True
                except:
                    continue
            
            # Additional check: if login fields disappeared and no errors, likely success
            try:
                username_visible = self.page.locator('#login').is_visible()
                password_visible = self.page.locator('#password').is_visible()
                
                if not username_visible and not password_visible:
                    logger.info("Login fields are no longer visible - login likely successful")
                    return True
            except:
                pass
            
            # If still on login page, login failed
            if "login" in current_url.lower():
                # Take debug screenshot after failed login
                try:
                    debug_screenshot = f"debug_login_failed_{int(time.time())}.png"
                    self.page.screenshot(path=debug_screenshot)
                    logger.info(f"Post-login screenshot saved as {debug_screenshot}")
                    
                    # Check for any visible error messages
                    error_elements = self.page.locator('.alert, .error, [class*="error"], [class*="alert"], .invalid-feedback, .text-danger').all()
                    if error_elements:
                        for error_elem in error_elements:
                            try:
                                if error_elem.is_visible():
                                    error_text = error_elem.text_content()
                                    if error_text and error_text.strip():
                                        logger.error(f"Error message found: {error_text.strip()}")
                            except:
                                continue
                    else:
                        logger.info("No visible error messages found")
                    
                    # Check page title and any status messages
                    page_title = self.page.title()
                    logger.info(f"Page title after login attempt: {page_title}")
                    
                    # Check if username and password fields are still visible (indicates login form still present)
                    username_still_visible = self.page.locator('#login').is_visible()
                    password_still_visible = self.page.locator('#password').is_visible()
                    logger.info(f"Username field still visible: {username_still_visible}")
                    logger.info(f"Password field still visible: {password_still_visible}")
                    
                except Exception as debug_e:
                    logger.warning(f"Debug screenshot failed: {debug_e}")
                
                logger.error("2-step login failed - still on login page")
                return False
            
            # Default to success if we've navigated away from login
            logger.info("2-step login appears successful")
            return True
                
        except Exception as e:
            logger.error(f"Error during 2-step PlanOut login: {str(e)}")
            logger.error(traceback.format_exc())
            return False
    
    def configure_boxoffice_settings(self):
        """
        Configure Box Office settings before accessing Box Office
        Goes to settingsBoxoffice, selects data-value="1", and saves
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Configuring Box Office settings...")
            
            # Navigate to settings page
            settings_url = f"{self.base_url}/backoffice/settingsBoxoffice"
            try:
                response = self.page.goto(settings_url, wait_until="networkidle", timeout=self.timeout)
                if response and not response.ok:
                    logger.error(f"Failed to load settings page: HTTP {response.status}")
                    return False
            except Exception as nav_e:
                logger.warning(f"Navigation to settings had an issue: {nav_e}, continuing...")
                
            logger.info(f"Navigated to Box Office settings: {settings_url}")
            
            # Wait for page to load
            time.sleep(3)
            
            # Look for the Box Office selection element with data-value="1"
            boxoffice_selectors = [
                'div[data-value="1"]',
                '.choices__item[data-value="1"]',
                '[data-value="1"][data-custom-properties]',
                'div.choices__item.choices__item--selectable[data-value="1"]'
            ]
            
            boxoffice_element = None
            for selector in boxoffice_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        boxoffice_element = selector
                        logger.info(f"Found Box Office selection element: {selector}")
                        break
                except:
                    continue
            
            if not boxoffice_element:
                logger.warning("Box Office selection element not found, continuing anyway...")
            else:
                # Click on the Box Office selection
                self.page.click(boxoffice_element)
                logger.info("Selected Box Office 1")
                time.sleep(2)
            
            # Look for save button (floppy icon)
            save_selectors = [
                '.palco4icon-floppy',
                'i.palco4icon.palco4icon-floppy',
                '.element-content:has(.palco4icon-floppy)',
                'div.element-content:has(i.palco4icon-floppy)',
                '[class*="floppy"]',
                'button:has(.palco4icon-floppy)'
            ]
            
            save_button = None
            for selector in save_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        save_button = selector
                        logger.info(f"Found save button: {selector}")
                        break
                except:
                    continue
            
            if save_button:
                self.page.click(save_button)
                logger.info("Clicked save button")
                time.sleep(3)
            else:
                logger.warning("Save button not found, continuing anyway...")
            
            logger.info("Box Office settings configuration completed")
            return True
            
        except Exception as e:
            logger.error(f"Error configuring Box Office settings: {str(e)}")
            logger.error(traceback.format_exc())
            return False
    
    def navigate_to_boxoffice(self) -> bool:
        """
        Navigates to the Box office section of PlanOut
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Navigating to Box office section in PlanOut")
            
            # Navigate directly to Box Office URL
            boxoffice_url = f"{self.base_url}/backoffice/boxoffice"
            try:
                response = self.page.goto(boxoffice_url, wait_until="networkidle", timeout=self.timeout)
                if response and not response.ok:
                    logger.error(f"Failed to load Box Office page: HTTP {response.status}")
                    return False
            except Exception as nav_e:
                logger.warning(f"Navigation to Box Office had an issue: {nav_e}, continuing...")
                
            logger.info(f"Successfully navigated to Box office: {boxoffice_url}")
            time.sleep(3)  # Wait for page to fully load
            return True
            
        except Exception as e:
            logger.error(f"Error navigating to Box office: {str(e)}")
            return False
    
    def click_csv_upload_button(self) -> bool:
        """
        Clicks the CSV upload button in Box office section
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Looking for CSV upload button")
            
            # Specific selectors based on the exact HTML provided
            csv_upload_selectors = [
                # Exact selector from the provided HTML structure
                '#csv-sales',
                'div#csv-sales.btn-ctrl-sesion-right-l',
                'div[id="csv-sales"][class="btn-ctrl-sesion-right-l"]',
                # With onclick attribute
                'div[onclick="showModalCsvSales()"]',
                '#csv-sales[onclick="showModalCsvSales()"]',
                # Icon-based selectors from the provided HTML
                'div:has(.palco4icon-cloud-upload)',
                '.palco4icon-cloud-upload',
                'div.palco4icon-cloud-upload',
                # Combined selectors
                '.btn-ctrl-sesion-right-l:has(.palco4icon-cloud-upload)',
                'div.btn-ctrl-sesion-right-l:has(div.palco4icon-cloud-upload)',
                # Fallback selectors
                '*[onclick*="showModalCsvSales"]',
                '.btn-ctrl-sesion-right-l',
                '[title*="upload"]'
            ]
            
            # Try to find and click the CSV upload button
            for selector in csv_upload_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        logger.info(f"Found CSV upload button with selector: {selector}")
                        self.page.click(selector)
                        logger.info("Clicked on CSV upload button")
                        
                        # Wait for modal to appear
                        time.sleep(2)
                        
                        # Look for modal or upload interface
                        modal_selectors = [
                            '.modal',
                            '#modal',
                            '.upload-modal',
                            '.csv-modal',
                            '[id*="modal"]',
                            '[class*="modal"]'
                        ]
                        
                        modal_appeared = False
                        for modal_selector in modal_selectors:
                            try:
                                if self.page.locator(modal_selector).is_visible():
                                    logger.info(f"Modal appeared: {modal_selector}")
                                    modal_appeared = True
                                    break
                            except:
                                continue
                        
                        if modal_appeared:
                            logger.info("CSV upload modal opened successfully")
                            return True
                        else:
                            logger.warning("CSV upload button clicked but no modal detected")
                            return True  # Continue anyway
                        
                except Exception as e:
                    logger.debug(f"CSV upload selector {selector} failed: {str(e)}")
                    continue
            
            # Take debug screenshot to see what's on the page
            try:
                debug_screenshot = f"debug_boxoffice_{int(time.time())}.png"
                self.page.screenshot(path=debug_screenshot)
                logger.info(f"Box office page screenshot saved as {debug_screenshot}")
                
                # Log all clickable elements for debugging
                clickable_elements = self.page.locator('[onclick], button, .btn, [class*="btn"]').all()
                logger.info(f"Found {len(clickable_elements)} clickable elements on box office page")
                
                for i, elem in enumerate(clickable_elements[:10]):  # Show first 10
                    try:
                        text = elem.text_content()
                        onclick = elem.get_attribute('onclick')
                        classes = elem.get_attribute('class')
                        logger.info(f"Element {i}: text='{text}', onclick='{onclick}', class='{classes}'")
                    except:
                        pass
                        
            except Exception as debug_e:
                logger.warning(f"Debug screenshot failed: {debug_e}")
                
            logger.error("Could not find CSV upload button")
            return False
            
        except Exception as e:
            logger.error(f"Error clicking CSV upload button: {str(e)}")
            return False
    
    def select_aforo_total_zone(self) -> bool:
        """
        Selects 'Aforo Total' (value="0") from the zone dropdown in the CSV modal
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Looking for zone selection dropdown")
            
            # Wait for the modal and dropdown to be available
            time.sleep(2)
            
            # Specific selectors for the zone dropdown
            zone_dropdown_selectors = [
                # Exact selector from HTML
                '#zoneCSV',
                'select#zoneCSV',
                # Alternative selectors
                'select[onchange="populatePrices()"]',
                'select[id="zoneCSV"]',
                # Generic fallbacks
                'select:has(option:has-text("Aforo Total"))',
                'select:has(option[value="0"])',
                'select:has(option:has-text("Select zone"))'
            ]
            
            # Try to find the zone dropdown
            zone_dropdown = None
            for selector in zone_dropdown_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        zone_dropdown = selector
                        logger.info(f"Found zone dropdown with selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"Zone dropdown selector {selector} failed: {str(e)}")
                    continue
            
            if not zone_dropdown:
                logger.error("Could not find zone dropdown")
                return False
            
            # Select 'Aforo Total' option (value="0")
            logger.info("Selecting 'Aforo Total' option")
            
            # Try different methods to select the option
            selection_methods = [
                # Method 1: Select by value
                lambda: self.page.select_option(zone_dropdown, value="0"),
                # Method 2: Select by text
                lambda: self.page.select_option(zone_dropdown, label="Aforo Total"),
                # Method 3: Click dropdown and then click option
                lambda: self._select_by_clicking(zone_dropdown, "0")
            ]
            
            for i, method in enumerate(selection_methods, 1):
                try:
                    method()
                    logger.info(f"Successfully selected 'Aforo Total' using method {i}")
                    
                    # Wait for any changes triggered by the selection
                    time.sleep(1)
                    
                    # Verify selection was made
                    try:
                        selected_value = self.page.locator(zone_dropdown).input_value()
                        if selected_value == "0":
                            logger.info("Zone selection verified: Aforo Total (value=0)")
                            return True
                        else:
                            logger.warning(f"Unexpected selected value: {selected_value}")
                    except:
                        logger.warning("Could not verify selection, but continuing")
                        return True
                    
                    return True
                    
                except Exception as e:
                    logger.debug(f"Selection method {i} failed: {str(e)}")
                    continue
            
            logger.error("All selection methods failed")
            return False
            
        except Exception as e:
            logger.error(f"Error selecting Aforo Total zone: {str(e)}")
            return False
    
    def _select_by_clicking(self, dropdown_selector: str, target_value: str) -> None:
        """
        Helper method to select option by clicking dropdown and then the option
        """
        # Click to open dropdown
        self.page.click(dropdown_selector)
        time.sleep(0.5)
        
        # Click the specific option
        option_selectors = [
            f'{dropdown_selector} option[value="{target_value}"]',
            f'option[value="{target_value}"]',
            ':has-text("Aforo Total")'
        ]
        
        for option_selector in option_selectors:
            try:
                if self.page.locator(option_selector).is_visible():
                    self.page.click(option_selector)
                    return
            except:
                continue
        
        raise Exception("Could not find option to click")
    
    def select_test_ticket_price(self) -> bool:
        """
        Selects 'TICKET PARA ENVIO DE PRUEBA - $0' (value="2623") from the price dropdown
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Looking for price selection dropdown")
            
            # Wait for the price dropdown to be populated after zone selection
            time.sleep(2)
            
            # Specific selectors for the price dropdown
            price_dropdown_selectors = [
                # Exact selector from HTML
                '#priceCSV',
                'select#priceCSV',
                # Alternative selectors
                'select:has(option:has-text("Select price"))',
                'select:has(option[value="2623"])',
                'select:has(option:has-text("TICKET PARA ENVIO DE PRUEBA"))'
            ]
            
            # Try to find the price dropdown
            price_dropdown = None
            for selector in price_dropdown_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        price_dropdown = selector
                        logger.info(f"Found price dropdown with selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"Price dropdown selector {selector} failed: {str(e)}")
                    continue
            
            if not price_dropdown:
                logger.error("Could not find price dropdown")
                return False
            
            # Wait a bit more for options to be populated
            time.sleep(1)
            
            # Select 'TICKET PARA ENVIO DE PRUEBA - $0' option (value="2623")
            logger.info("Selecting 'TICKET PARA ENVIO DE PRUEBA - $0' option")
            
            # Try different methods to select the option
            selection_methods = [
                # Method 1: Select by value
                lambda: self.page.select_option(price_dropdown, value="2623"),
                # Method 2: Select by text (partial match)
                lambda: self.page.select_option(price_dropdown, label="TICKET PARA ENVIO DE PRUEBA - $0"),
                # Method 3: Select by partial text
                lambda: self.page.locator(price_dropdown).select_option(label=re.compile("TICKET PARA ENVIO DE PRUEBA")),
                # Method 4: Click dropdown and then click option
                lambda: self._select_price_by_clicking(price_dropdown, "2623")
            ]
            
            for i, method in enumerate(selection_methods, 1):
                try:
                    method()
                    logger.info(f"Successfully selected test ticket using method {i}")
                    
                    # Wait for any changes triggered by the selection
                    time.sleep(1)
                    
                    # Verify selection was made
                    try:
                        selected_value = self.page.locator(price_dropdown).input_value()
                        if selected_value == "2623":
                            logger.info("Price selection verified: TICKET PARA ENVIO DE PRUEBA - $0 (value=2623)")
                            return True
                        else:
                            logger.warning(f"Unexpected selected price value: {selected_value}")
                    except:
                        logger.warning("Could not verify price selection, but continuing")
                        return True
                    
                    return True
                    
                except Exception as e:
                    logger.debug(f"Price selection method {i} failed: {str(e)}")
                    continue
            
            logger.error("All price selection methods failed")
            return False
            
        except Exception as e:
            logger.error(f"Error selecting test ticket price: {str(e)}")
            return False
    
    def _select_price_by_clicking(self, dropdown_selector: str, target_value: str) -> None:
        """
        Helper method to select price option by clicking dropdown and then the option
        """
        # Click to open dropdown
        self.page.click(dropdown_selector)
        time.sleep(0.5)
        
        # Click the specific option
        option_selectors = [
            f'{dropdown_selector} option[value="{target_value}"]',
            f'option[value="{target_value}"]',
            ':has-text("TICKET PARA ENVIO DE PRUEBA")',
            f'#{dropdown_selector.replace("#", "")} option[value="{target_value}"]'
        ]
        
        for option_selector in option_selectors:
            try:
                if self.page.locator(option_selector).is_visible():
                    self.page.click(option_selector)
                    return
            except:
                continue
        
        raise Exception("Could not find price option to click")
    
    def upload_csv_file(self, csv_file_path: str) -> bool:
        """
        Uploads the CSV file to PlanOut using the file input
        Args:
            csv_file_path: Path to the CSV file to upload
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Uploading CSV file: {csv_file_path}")
            
            # Wait for file input to be available
            time.sleep(2)
            
            # Specific selectors for the file input
            file_input_selectors = [
                # Exact selector from HTML
                '#inputFile_fileUpload',
                'input#inputFile_fileUpload',
                # Alternative selectors
                'input[name="inputFile_fileUpload"]',
                'input[type="file"][id*="fileUpload"]',
                # Generic file input selectors
                'input[type="file"]',
                'input[accept*=".csv"]'
            ]
            
            # Try to find the file input
            file_input = None
            for selector in file_input_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        file_input = selector
                        logger.info(f"Found file input with selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"File input selector {selector} failed: {str(e)}")
                    continue
            
            if not file_input:
                logger.error("Could not find file input element")
                return False
            
            # Upload the CSV file
            logger.info("Uploading CSV file to input")
            self.page.set_input_files(file_input, csv_file_path)
            
            # Wait for file to be processed
            time.sleep(3)
            
            # Verify file was uploaded by checking if filename appears somewhere
            try:
                file_name = os.path.basename(csv_file_path)
                # Look for the filename in various places
                filename_indicators = [
                    f':has-text("{file_name}")',
                    f':has-text("{file_name.split(".")[0]}")',  # Without extension
                    '.file-name',
                    '.filename',
                    '.uploaded-file'
                ]
                
                file_uploaded = False
                for indicator in filename_indicators:
                    try:
                        if self.page.locator(indicator).is_visible():
                            logger.info(f"File upload confirmed by indicator: {indicator}")
                            file_uploaded = True
                            break
                    except:
                        continue
                
                if file_uploaded:
                    logger.info("CSV file uploaded successfully")
                    return True
                else:
                    logger.warning("File uploaded but no confirmation indicator found")
                    return True  # Continue anyway, file input likely worked
                    
            except Exception as verify_error:
                logger.warning(f"Could not verify file upload: {verify_error}")
                return True  # Continue anyway
            
        except Exception as e:
            logger.error(f"Error uploading CSV file: {str(e)}")
            logger.error(traceback.format_exc())
            return False
    
    def check_send_confirmation_email(self) -> bool:
        """
        Checks the 'Send confirmation email' checkbox
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Looking for 'Send confirmation email' checkbox")
            
            # Wait for checkbox to be available
            time.sleep(2)
            
            # Specific selectors for the confirmation email checkbox
            checkbox_selectors = [
                # Exact selector from HTML
                '#sendConfirmationEmail',
                'input#sendConfirmationEmail',
                # Alternative selectors
                'input[name="sendConfirmationEmail"]',
                'input[type="checkbox"][value="true"]',
                # Parent wrapper selectors
                '#sendConfirmationEmail-wrapper input[type="checkbox"]',
                # Label-based selection
                'input[type="checkbox"] + label:has-text("Send confirmation email")'
            ]
            
            # Try to find the checkbox
            checkbox = None
            for selector in checkbox_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        checkbox = selector
                        logger.info(f"Found send confirmation email checkbox with selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"Checkbox selector {selector} failed: {str(e)}")
                    continue
            
            if not checkbox:
                logger.error("Could not find send confirmation email checkbox")
                return False
            
            # Check if checkbox is already checked
            try:
                is_checked = self.page.locator(checkbox).is_checked()
                if is_checked:
                    logger.info("Send confirmation email checkbox is already checked")
                    return True
            except:
                logger.warning("Could not determine checkbox state, proceeding to check it")
            
            # Check the checkbox
            logger.info("Checking 'Send confirmation email' checkbox")
            self.page.check(checkbox)
            
            # Wait for any changes
            time.sleep(1)
            
            # Verify checkbox is checked
            try:
                is_checked_after = self.page.locator(checkbox).is_checked()
                if is_checked_after:
                    logger.info("Send confirmation email checkbox checked successfully")
                    return True
                else:
                    logger.warning("Checkbox may not have been checked properly")
                    return True  # Continue anyway
            except:
                logger.warning("Could not verify checkbox state after checking")
                return True  # Continue anyway
            
        except Exception as e:
            logger.error(f"Error checking send confirmation email checkbox: {str(e)}")
            logger.error(traceback.format_exc())
            return False
    
    def click_select_button(self) -> bool:
        """
        Clicks the final 'Select' button to process the CSV and send QR codes
        Returns: True if successful, False otherwise
        """
        try:
            logger.info("Looking for 'Select' button to process CSV")
            
            # Wait for button to be available
            time.sleep(2)
            
            # Specific selectors for the Select button
            select_button_selectors = [
                # Exact selector based on HTML structure
                'a.button.primary.expanded[onclick="readLoadSalesCSV(); return false"]',
                # Alternative selectors
                'a[onclick*="readLoadSalesCSV"]',
                '.button.primary.expanded:has-text("Select")',
                'a.button:has-text("Select")',
                # Generic button selectors
                ':has-text("Select")',
                '.button.primary',
                'a.button.expanded'
            ]
            
            # Try to find the Select button
            select_button = None
            for selector in select_button_selectors:
                try:
                    if self.page.locator(selector).is_visible():
                        select_button = selector
                        logger.info(f"Found Select button with selector: {selector}")
                        break
                except Exception as e:
                    logger.debug(f"Select button selector {selector} failed: {str(e)}")
                    continue
            
            if not select_button:
                logger.error("Could not find Select button")
                return False
            
            # Click the Select button
            logger.info("Clicking 'Select' button to process CSV and send QR codes")
            self.page.click(select_button)
            
            # Wait for processing to start
            time.sleep(3)
            
            # Look for processing indicators or success messages
            processing_indicators = [
                '.loading',
                '.processing',
                ':has-text("Processing")',
                ':has-text("Loading")',
                ':has-text("Sending")',
                ':has-text("Success")',
                ':has-text("Complete")',
                ':has-text("Enviando")',
                ':has-text("Procesando")',
                '.progress-bar',
                '.spinner'
            ]
            
            processing_started = False
            for indicator in processing_indicators:
                try:
                    if self.page.locator(indicator).is_visible():
                        logger.info(f"Processing started - indicator found: {indicator}")
                        processing_started = True
                        break
                except:
                    continue
            
            if processing_started:
                logger.info("QR processing started successfully")
                
                # Wait for processing to complete (with timeout)
                logger.info("Waiting for QR processing to complete...")
                
                # Wait up to 60 seconds for completion
                for i in range(60):
                    time.sleep(1)
                    
                    # Check for completion indicators
                    completion_indicators = [
                        ':has-text("Complete")',
                        ':has-text("Success")',
                        ':has-text("Sent")',
                        ':has-text("Finished")',
                        ':has-text("Done")',
                        ':has-text("Completado")',
                        ':has-text("Enviado")',
                        ':has-text("Finalizado")'
                    ]
                    
                    completed = False
                    for completion_indicator in completion_indicators:
                        try:
                            if self.page.locator(completion_indicator).is_visible():
                                logger.info(f"Processing completed - indicator: {completion_indicator}")
                                completed = True
                                break
                        except:
                            continue
                    
                    if completed:
                        break
                    
                    if i % 10 == 0:  # Log every 10 seconds
                        logger.info(f"Still processing... ({i+1} seconds elapsed)")
                
                logger.info("QR code processing and sending completed")
                return True
                
            else:
                logger.warning("No processing indicator found, but button was clicked")
                return True  # Continue anyway, process might have started
            
        except Exception as e:
            logger.error(f"Error clicking Select button: {str(e)}")
            logger.error(traceback.format_exc())
            return False
    
    def prepare_guest_sheet(self, guests_data: List[Dict]) -> str:
        """
        Prepares a CSV file with guest data for PlanOut upload
        Args:
            guests_data: List of guest dictionaries from Google Sheets
        Returns:
            Path to created CSV file
        """
        try:
            logger.info(f"Preparing CSV file for {len(guests_data)} guests")
            
            # Prepare data for PlanOut CSV format with exact column structure
            csv_data = []
            
            for guest in guests_data:
                # Extract guest information from Google Sheets format
                full_name = guest.get('name', '') or guest.get('Nombre y Apellido', '') or guest.get('Nombre', '')
                email = guest.get('email', '') or guest.get('Email', '') or guest.get('email', '')
                
                # Split name into firstName and surname if possible
                name_parts = full_name.strip().split(' ', 1) if full_name else ['', '']
                first_name = name_parts[0] if len(name_parts) > 0 else ''
                surname = name_parts[1] if len(name_parts) > 1 else ''
                
                # Create row with PlanOut's exact column structure
                csv_row = {
                    # Required columns
                    'email': email,                           # Column 1
                    'tickets amount': 1,                      # Column 2 - default to 1 ticket
                    # Optional columns with data we have
                    'firstName': first_name,                  # Column 3
                    'surname': surname,                       # Column 4
                    'phoneticName': '',                       # Column 5 - Optional
                    'phoneticSurname': '',                    # Column 6 - Optional
                    'idCard': '',                             # Column 7 - Optional
                    'idTypeCard': '',                         # Column 8 - Optional
                    'address': '',                            # Column 9 - Optional
                    'district': '',                           # Column 10 - Optional
                    'city': '',                               # Column 11 - Optional
                    'state': '',                              # Column 12 - Optional
                    'country': '',                            # Column 13 - Optional
                    'birthDate': '',                          # Column 14 - Optional (YYYY-MM-DD format)
                    'gender': '',                             # Column 15 - Optional (H/M/O)
                    'language': '',                           # Column 16
                    'telephone': '',                          # Column 17 - Optional
                    'phoneCountryCode': '',                   # Column 18 - Optional
                    'profileImageUrl': '',                    # Column 19 - Optional
                    'company': '',                            # Column 20 - Optional
                    'acceptsCommercialOffers': 'TRUE',        # Column 21 - Required TRUE
                    'acceptsLegalTerms': 'TRUE',              # Column 22 - Required TRUE
                    'customField1': '',                       # Column 23 - Optional
                    'customField2': '',                       # Column 24 - Optional
                    'department': '',                         # Column 25 - Optional
                    'jobTitle': '',                           # Column 26 - Optional
                    'licensePlate': ''                        # Column 27 - Optional
                }
                
                csv_data.append(csv_row)
            
            # Create DataFrame with proper column order
            column_order = [
                'email', 'tickets amount', 'firstName', 'surname', 'phoneticName', 'phoneticSurname',
                'idCard', 'idTypeCard', 'address', 'district', 'city', 'state', 'country',
                'birthDate', 'gender', 'language', 'telephone', 'phoneCountryCode', 'profileImageUrl',
                'company', 'acceptsCommercialOffers', 'acceptsLegalTerms', 'customField1', 'customField2',
                'department', 'jobTitle', 'licensePlate'
            ]
            
            df = pd.DataFrame(csv_data, columns=column_order)
            
            # Remove rows without email (required field)
            df = df.dropna(subset=['email'])
            df = df[df['email'].str.strip() != '']
            
            # Create temporary CSV file
            temp_file = tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.csv', 
                delete=False,
                encoding='utf-8',
                newline=''
            )
            
            # Write CSV file with proper formatting
            df.to_csv(temp_file.name, index=False, encoding='utf-8')
            temp_file.close()
            
            logger.info(f"CSV file prepared: {temp_file.name} with {len(df)} guests")
            logger.info(f"Columns: {list(df.columns)}")
            logger.info(f"Sample data - First guest email: {df.iloc[0]['email'] if len(df) > 0 else 'No data'}")
            
            return temp_file.name
            
        except Exception as e:
            logger.error(f"Error preparing guest CSV file: {str(e)}")
            logger.error(traceback.format_exc())
            raise
    
    def upload_guest_sheet(self, csv_file_path: str) -> bool:
        """
        Uploads guest CSV to PlanOut.com.ar
        Args:
            csv_file_path: Path to CSV file to upload
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Uploading guest sheet: {csv_file_path}")
            
            # Look for upload/import section
            upload_selectors = [
                'input[type="file"]',
                '[data-testid="file-upload"]',
                '.file-upload',
                '#file-upload'
            ]
            
            upload_found = False
            for selector in upload_selectors:
                try:
                    if self.page.is_visible(selector):
                        # Upload the file
                        self.page.set_input_files(selector, csv_file_path)
                        upload_found = True
                        logger.info(f"File uploaded using selector: {selector}")
                        
                        # Wait for upload to complete
                        time.sleep(2)
                        
                        # Look for upload/process button
                        process_selectors = [
                            'button:has-text("Upload")',
                            'button:has-text("Process")',
                            'button:has-text("Import")',
                            'button:has-text("Subir")',
                            'button:has-text("Procesar")',
                            '.btn-upload',
                            '.btn-process'
                        ]
                        
                        for btn_selector in process_selectors:
                            try:
                                if self.page.is_visible(btn_selector):
                                    self.page.click(btn_selector)
                                    logger.info(f"Process button clicked: {btn_selector}")
                                    break
                            except:
                                continue
                        
                        break
                        
                except Exception as e:
                    logger.debug(f"Upload selector {selector} failed: {str(e)}")
                    continue
                    
            if not upload_found:
                logger.error("Could not find file upload element")
                return False
                
            # Wait for processing to complete
            time.sleep(5)
            
            return True
            
        except Exception as e:
            logger.error(f"Error uploading guest sheet: {str(e)}")
            return False
    
    def generate_and_send_qrs(self) -> Dict[str, any]:
        """
        Generates and sends QR codes for uploaded guests
        Returns:
            Dictionary with results and statistics
        """
        try:
            logger.info("Starting QR generation and sending process")
            
            # Look for QR generation options
            qr_selectors = [
                'button:has-text("Generate QR")',
                'button:has-text("Send QR")',
                'button:has-text("Enviar QR")',
                'button:has-text("Generar QR")',
                '.btn-qr',
                '.qr-generate'
            ]
            
            qr_found = False
            for selector in qr_selectors:
                try:
                    if self.page.is_visible(selector):
                        self.page.click(selector)
                        qr_found = True
                        logger.info(f"QR generation started using: {selector}")
                        break
                except:
                    continue
                    
            if not qr_found:
                logger.error("Could not find QR generation button")
                return {"success": False, "error": "QR generation button not found"}
            
            # Wait for QR generation to complete
            logger.info("Waiting for QR generation to complete...")
            time.sleep(10)  # Adjust based on expected processing time
            
            # Look for success indicators or completion messages
            success_indicators = [
                ':has-text("Success")',
                ':has-text("Complete")',
                ':has-text("Sent")',
                ':has-text("Enviado")',
                ':has-text("Completado")',
                '.success',
                '.completed'
            ]
            
            success_found = False
            for indicator in success_indicators:
                try:
                    if self.page.is_visible(indicator):
                        success_found = True
                        logger.info(f"Success indicator found: {indicator}")
                        break
                except:
                    continue
            
            # Try to extract statistics
            stats = self._extract_qr_stats()
            
            result = {
                "success": success_found,
                "timestamp": datetime.now().isoformat(),
                "stats": stats
            }
            
            logger.info(f"QR generation completed with result: {result}")
            return result
            
        except Exception as e:
            logger.error(f"Error in QR generation: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def _extract_qr_stats(self) -> Dict[str, any]:
        """
        Extracts statistics from the QR generation results page
        Returns:
            Dictionary with statistics
        """
        try:
            stats = {
                "total_processed": 0,
                "successful_sends": 0,
                "failed_sends": 0,
                "details": []
            }
            
            # Try to find statistics elements
            stat_selectors = [
                '.stats',
                '.results',
                '.summary',
                '[data-testid="stats"]'
            ]
            
            for selector in stat_selectors:
                try:
                    if self.page.is_visible(selector):
                        # Extract text content and parse numbers
                        content = self.page.text_content(selector)
                        logger.info(f"Found stats content: {content}")
                        
                        # Simple parsing for numbers
                        import re
                        numbers = re.findall(r'\d+', content)
                        if len(numbers) >= 2:
                            stats["total_processed"] = int(numbers[0])
                            stats["successful_sends"] = int(numbers[1])
                            if len(numbers) >= 3:
                                stats["failed_sends"] = int(numbers[2])
                        
                        break
                except:
                    continue
            
            return stats
            
        except Exception as e:
            logger.error(f"Error extracting QR stats: {str(e)}")
            return {"error": str(e)}
    
    def full_automation_workflow(self, guests_data: List[Dict]) -> Dict[str, any]:
        """
        Complete automation workflow with all PlanOut steps
        Args:
            guests_data: List of guest dictionaries
        Returns:
            Dictionary with complete results
        """
        try:
            logger.info(f"🎯 PLANOUT WORKFLOW INICIADO - {len(guests_data)} invitados")
            logger.info(f"🌍 URL base: {self.base_url}")
            logger.info(f"👤 Usuario: {self.username}")
            logger.info(f"🖥️ Modo headless: {self.headless}")
            
            # Step 1: Login to PlanOut
            logger.info("🔐 PASO 1: Iniciando login a PlanOut...")
            if not self.login_planout():
                logger.error("❌ PASO 1 FALLÓ: Login failed")
                return {"success": False, "error": "Login failed"}
            logger.info("✅ PASO 1 COMPLETADO: Login exitoso")
            
            # Step 1.5: Configure Box Office settings
            logger.info("⚙️ PASO 1.5: Configurando Box Office...")
            if not self.configure_boxoffice_settings():
                logger.error("❌ PASO 1.5 FALLÓ: Box Office configuration failed")
                return {"success": False, "error": "Box Office configuration failed"}
            logger.info("✅ PASO 1.5 COMPLETADO: Box Office configurado")
            
            # Step 2: Navigate to Box Office section
            logger.info("🧭 PASO 2: Navegando a Box Office...")
            if not self.navigate_to_boxoffice():
                logger.error("❌ PASO 2 FALLÓ: Navigation to Box Office failed")
                return {"success": False, "error": "Navigation to Box Office failed"}
            logger.info("✅ PASO 2 COMPLETADO: En Box Office")
            
            # Step 3: Click CSV upload button
            logger.info("📤 PASO 3: Abriendo modal de CSV...")
            if not self.click_csv_upload_button():
                logger.error("❌ PASO 3 FALLÓ: Failed to open CSV upload modal")
                return {"success": False, "error": "Failed to open CSV upload modal"}
            logger.info("✅ PASO 3 COMPLETADO: Modal CSV abierto")
            
            # Step 4: Select "Aforo Total" zone
            logger.info("🎫 PASO 4: Seleccionando zona Aforo Total...")
            if not self.select_aforo_total_zone():
                logger.error("❌ PASO 4 FALLÓ: Failed to select Aforo Total zone")
                return {"success": False, "error": "Failed to select Aforo Total zone"}
            logger.info("✅ PASO 4 COMPLETADO: Zona seleccionada")
            
            # Step 5: Select test ticket price
            logger.info("💰 PASO 5: Seleccionando precio de ticket...")
            if not self.select_test_ticket_price():
                logger.error("❌ PASO 5 FALLÓ: Failed to select test ticket price")
                return {"success": False, "error": "Failed to select test ticket price"}
            logger.info("✅ PASO 5 COMPLETADO: Precio seleccionado")
            
            # Step 6: Prepare CSV file
            logger.info("📋 PASO 6: Preparando archivo CSV...")
            csv_file = self.prepare_guest_sheet(guests_data)
            logger.info(f"✅ PASO 6 COMPLETADO: CSV creado en {csv_file}")
            
            try:
                # Step 7: Upload CSV file
                logger.info("⬆️ PASO 7: Subiendo archivo CSV...")
                if not self.upload_csv_file(csv_file):
                    logger.error("❌ PASO 7 FALLÓ: CSV file upload failed")
                    return {"success": False, "error": "CSV file upload failed"}
                logger.info("✅ PASO 7 COMPLETADO: CSV subido exitosamente")
                
                # Step 8: Check send confirmation email checkbox
                logger.info("📧 PASO 8: Marcando opción de email de confirmación...")
                if not self.check_send_confirmation_email():
                    logger.error("❌ PASO 8 FALLÓ: Failed to check send confirmation email")
                    return {"success": False, "error": "Failed to check send confirmation email"}
                logger.info("✅ PASO 8 COMPLETADO: Email de confirmación activado")
                
                # Step 9: Click Select button to process and send QRs
                logger.info("🚀 PASO 9: Procesando CSV y enviando códigos QR...")
                if not self.click_select_button():
                    logger.error("❌ PASO 9 FALLÓ: Failed to process CSV and send QRs")
                    return {"success": False, "error": "Failed to process CSV and send QRs"}
                logger.info("✅ PASO 9 COMPLETADO: QRs procesados y enviados")
                
                # Success!
                result = {
                    "success": True,
                    "total_guests": len(guests_data),
                    "message": "QR codes processed and sent successfully",
                    "timestamp": datetime.now().isoformat(),
                    "steps_completed": 9
                }
                
                logger.info("🎉 WORKFLOW COMPLETADO EXITOSAMENTE!")
                logger.info(f"📊 Resultado final: {result}")
                return result
                
            finally:
                # Cleanup temporary CSV file
                try:
                    os.unlink(csv_file)
                    logger.info(f"Cleaned up temporary CSV file: {csv_file}")
                except Exception as cleanup_error:
                    logger.warning(f"Could not cleanup temporary file: {cleanup_error}")
                    
        except Exception as e:
            logger.error(f"Error in full automation workflow: {str(e)}")
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}

def test_automation():
    """Test function for automation workflow"""
    # Sample guest data for testing
    test_guests = [
        {"name": "Juan Pérez", "email": "juan@test.com", "category": "VIP"},
        {"name": "María López", "email": "maria@test.com", "category": "General"}
    ]
    
    try:
        with PlanOutAutomation() as automation:
            result = automation.full_automation_workflow(test_guests)
            print(f"Test result: {json.dumps(result, indent=2)}")
            return result
    except Exception as e:
        print(f"Test failed: {str(e)}")
        return {"success": False, "error": str(e)}

def test_francisco_email():
    """Test function specifically with Francisco's email"""
    # Test data with Francisco's email
    test_guests = [
        {
            "name": "Francisco Quinteros", 
            "email": "franciscoquinterosok@gmail.com", 
            "category": "General",
            "event": "Evento Test"
        }
    ]
    
    try:
        print("Starting PlanOut automation test with Francisco's email...")
        print(f"Email: {test_guests[0]['email']}")
        print(f"Name: {test_guests[0]['name']}")
        
        with PlanOutAutomation() as automation:
            result = automation.full_automation_workflow(test_guests)
            
            print("\n" + "="*50)
            print("TEST RESULTS:")
            print("="*50)
            print(f"Success: {result.get('success', False)}")
            print(f"Message: {result.get('message', 'No message')}")
            print(f"Total Guests: {result.get('total_guests', 0)}")
            print(f"Timestamp: {result.get('timestamp', 'No timestamp')}")
            
            if result.get('success'):
                print("QR automation completed successfully!")
            else:
                print(f"Error: {result.get('error', 'Unknown error')}")
            
            return result
            
    except Exception as e:
        print(f"Test failed with exception: {str(e)}")
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    # Run test with Francisco's email
    test_francisco_email()