*** Settings ***
Documentation       Collection of common keywords to use across tests/suites
# Library             Browser     timeout=150s
Library    HealingSelenium.py    model=mistral:latest   auto_heal=${TRUE}    auto_rewrite=${TRUE}    ai=${FALSE}    non_ai=${TRUE}    heal_from_history=${FALSE}
Library    String
Library    Collections
Library    OperatingSystem
# Library             SeleniumLibrary
Suite Setup    Setup
Suite Teardown    Close Browser

*** Keywords ***

Click Continue Button
    [Documentation]    Click Continue Button
    Click Element    xpath=//button[@id="workflowContinueBtn-button"][@aria-disabled="false"]


Can Talk To Ollama
    ${status}=    Test Ollama Connection
    Should Be Equal As Integers    ${status}    200

Setup
    Test Ollama Connection
    # Open Browser    file:xpath=///Users/febinthomas/Desktop/hw/AI%20Based/SamplePage.html    browser=chrome
    ${chrome_options}     Evaluate    selenium.webdriver.ChromeOptions()    modules=selenium.webdriver
    Call Method    ${chrome_options}    add_argument    --no-sandbox
    Call Method    ${chrome_options}    add_argument    --disable-gpu
    Call Method    ${chrome_options}    add_argument    --incognito    
    Call Method    ${chrome_options}    add_argument    --ignore-certificate-errors
    Open Browser    https://the-internet.herokuapp.com/login    browser=chrome    options=${chrome_options}
    Maximize Browser Window

Colour CSV To Variable
    ${lines}=    Get File    colourMap.csv
    @{rows}=     Split To Lines    ${lines}
    FOR    ${row}    IN    @{rows}
        ${columns}=     Split String    ${row}    ,
        ${raw_name}=    Strip String    ${columns}[0]
        ${LIST_NAME}=   Convert To Upper Case    ${raw_name}
        @{VALUES}=      Get Slice From List    ${columns}    1
        VAR    @{${LIST_NAME}}    @{VALUES}    scope=global
    END

*** Test Cases ***

Verify login with valid credentials
    [Tags]    HappyCase    TC_1
    [Documentation]    Verify login with valid credentials
    Input Text    id=username    tomsmith
    Input Text    id=password    SuperSecretPassword!
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash success"]    10
    Element Should Be Visible    xpath=//div[@class="flash success"]
    Click Element    xpath=//i[text()=" Logout"]

Verify login with invalid username
    [Documentation]    Verify login with invalid username
    [Tags]    TC_2
    Input Text    id=username    user@name.com
    Input Text    id=password    SuperSecretPassword!
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify login with invalid password
    [Documentation]    Verify login with invalid password
    [Tags]    TC_3
    Input Text    id=username    tomsmith
    Input Text    id=password    Password
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify login with both invalid username and password
    [Documentation]    Verify login with both invalid username and password
    [Tags]    TC_4
    Input Text    id=username    user@name.com
    Input Text    id=password    Password
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify login with empty username and password
    [Documentation]    Verify login with empty username and password
    [Tags]    TC_5
    Clear Element Text    xpath=//input[@id="username"]
    Clear Element Text    xpath=//input[@id="password"]
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify login with empty username only
    [Documentation]    Verify login with empty username only
    [Tags]    TC_6
    Clear Element Text    xpath=//input[@id="username"]
    Input Text    id=password    SuperSecretPassword!
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify login with empty password only
    [Documentation]    Verify login with invalid username
    [Tags]    TC_7
    Input Text    id=username    user@name.com
    Clear Element Text    xpath=//input[@id="password"]
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]

Verify password masking
    [Documentation]    Verify password masking
    [Tags]    TC_8
    Input Text    xpath=//input[@id="username"]    user@name.com
    Input Text    xpath=//input[@id="password"]    SuperSecretPassword!
    Element Should Be Visible    xpath=//input[@id="password"][@type="password"]

Verify logout functionality
    [Documentation]    Verify logout functionality
    [Tags]    TC_9
    Input Text    id=username    tomsmith
    Input Text    id=password    SuperSecretPassword!
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash success"]    10
    Element Should Be Visible    xpath=//div[@class="flash success"]
    Click Element    xpath=//i[text()=" Logout"]

Verify SQL Injection attempt
    [Documentation]    Verify SQL Injection attempt
    [Tags]    TC_10
    Input Text    id=username    ' OR '1'='1
    Input Text    xpath=//input[@id="password"]    ' OR '1'='1
    Click Element   xpath=//button[@type="submit"]
    Wait Until Element Is Visible    xpath=//div[@class="flash error"]    10
    Element Should Be Visible    xpath=//div[@class="flash error"]