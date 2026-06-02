*** Settings ***
Documentation       Collection of common keywords to use across tests/suites
# Library             Browser     timeout=150s
Library    HealingSelenium.py    model=llama3.1:8b   auto_heal=${TRUE}    auto_rewrite=${TRUE}
Library    String
Library    Collections

# Library             SeleniumLibrary

*** Keywords ***

Click Continue Button
    [Documentation]    Click Continue Button
    Click Element    //button[@id="workflowContinueBtn-button"][@aria-disabled="false"]


Can Talk To Ollama
    ${status}=    Test Ollama Connection
    Should Be Equal As Integers    ${status}    200

Enter ID Into HomeView Search
    [Documentation]    Search for ID in HomeView
    [Arguments]    ${SerialNumber}
    ${textBox}    Set Variable    //input[@id="textInput-textInput"]
    Click Element    ${textBox}
    Clear Element Text    ${textBox}
    Input Text    ${textBox}    ${SerialNumber}
    Click Continue Button
    Sleep    2

TryNew
    ${ExtenderDict}    Create Dictionary    Ext1    WiFi Extender 7 Plus +028623+251200...
    ${ExtenderName}    Get From Dictionary   ${ExtenderDict}    Ext1
    Log    ${ExtenderName}    console=true


*** Test Cases ***
TC_1
    [Tags]    Selenium
    Can Talk To Ollama
    Open Browser    https://pbtcsc.saas.nokia.com/portal/btcspnonsso    browser=chrome
    # Open Browser    file:///Users/febinthomas/Desktop/hw/AI%20Based/SamplePage.html    browser=chrome    #executable_path=${CURDIR}/chromedriver
    Maximize Browser Window
    Set Selenium Implicit Wait    30
    Input Text    //input[@name="username"]    team.sentinel@bt.com
    Input Text    xpath=//input[@name='password']    SuNri1CZ
    Click Element    //input[@name="login"]
    # Enter ID Into HomeView Search    01473611975
    # Set Selenium Implicit Wait    10
    # # Wait Until Element Is Not Visible    //label[text()="Please wait.."]    100
    # # Select From List By Index    //ul[@class="list__items-wrapper"]    0
    # Click Element    //div[@class="Select-value"]
    # Sleep    1
    # Capture Page Screenshot
    # Press Keys    None    RETURN
    # Click Element    //span[@class="Select-value-label"][1]
    # Click Element    //li[@id="csfWidgets-Selectitem-1-list-0"]    #Working
    # Click Element    class=list__item
    # Select From List By Index    class=list__items-wrapper    0
    # Click Element    //div[@class="Select-control"]
    # Click Element    //div[@id="workflowContinueBtn-content"]

TC_2
    [Tags]    new
    Test Ollama Connection
    # Test Ollama Connection
    Open Browser    file:///Users/febinthomas/Desktop/hw/AI%20Based/SamplePage.html    browser=chrome
    Maximize Browser Window
    Input Text    xpath=//input[@name='username']    team.sentinel@bt.com
    Input Text    name=password    SuNri1CZ
    Click Element   id=login

TC_3
    [Tags]    new
    Sleep    5
    Input Text    //input[@name="username"]    team.sentinel@bt.com
    Input Text    xpath=//input[@id='username']    SuNri1CZ
    Click Element   id=login

TC_4
    [Tags]    interops
    
    ${var}    Set Variable    "[SUM]   0.00-120.06 sec  1.11 GBytes  79.5 Mbits/sec                  receiver"
    ${ext}    Get Regexp Matches    ${var}    (\\d+\\s+Mbits/sec)
    Log    ${ext}

Test
    TryNew