*** Settings ***
Documentation       Collection of common keywords to use across tests/suites
# Library             Browser     timeout=150s
Library    HealingSelenium.py    model=mistral:latest   auto_heal=${TRUE}    auto_rewrite=${TRUE}
Library    String
Library    Collections
Library    OperatingSystem
# Library             SeleniumLibrary
Suite Setup    Setup
Suite Teardown    Close Browser

*** Keywords ***

Click Continue Button
    [Documentation]    Click Continue Button
    Click Element    //button[@id="workflowContinueBtn-button"][@aria-disabled="false"]


Can Talk To Ollama
    ${status}=    Test Ollama Connection
    Should Be Equal As Integers    ${status}    200

Setup
    Test Ollama Connection
    # Open Browser    file:///Users/febinthomas/Desktop/hw/AI%20Based/SamplePage.html    browser=chrome
    Open Browser    https://the-internet.herokuapp.com/login    browser=chrome
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


TC_1
    Input Text    name=username    user@name.com
    Input Text    //input[@id="password"]    Password
    Click Element   //button[@type='submit']

TC_2
    Input Text    name=username    user@name.com
    Input Text    //input[@id="password"]    Password
    Click Element   //button[@type='submit']

TC_3
    [Tags]    HappyCase
    Input Text    name=username    tomsmith
    Input Text    //input[@id="password"]    SuperSecretPassword!
    Click Element   //button[@type='submit']